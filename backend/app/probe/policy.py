from __future__ import annotations

from contextlib import contextmanager
import os
from contextvars import ContextVar
from typing import Callable, Iterator

from .errors import ProbeDisabledError, ProbeError


# Probe admission is opt-in per async flow. A normal quote, API request, test
# harness, or background task cannot fall through to a state-writing Probe.
_ACTIVE_PROBE_CONTEXT: ContextVar[bool] = ContextVar("active_probe_context", default=False)
_PROBE_LEASE_CONTEXT: ContextVar[bool] = ContextVar("probe_lease_context", default=False)
_PROBE_RISK_CONTEXT: ContextVar[bool] = ContextVar("probe_risk_context", default=False)
_PROBE_RECOVERY_CONTEXT: ContextVar[bool] = ContextVar("probe_recovery_context", default=False)


class ProbePolicy:
    @classmethod
    def from_settings(
        cls,
        settings: object | Callable[[], object],
        *,
        active_probe_enabled: bool | None = None,
        external_writes_enabled: bool | None = None,
    ) -> "ProbePolicy":
        provider = settings if callable(settings) else lambda: settings
        current = provider()
        policy = cls(
            active_probe_enabled=bool(getattr(current, "wanda_active_probe_enabled", False)),
            external_writes_enabled=bool(getattr(current, "external_writes_enabled", False)),
            settings_provider=provider,
        )
        policy._active_probe_override = active_probe_enabled
        policy._external_writes_override = external_writes_enabled
        return policy

    def __init__(
        self,
        *,
        active_probe_enabled: bool = False,
        release_recheck_delays_seconds: tuple[float, ...] = (0.0, 2.0, 5.0),
        reconciliation_delays_seconds: tuple[float, ...] = (15.0, 15.0),
        account_lease_ttl_seconds: float = 180.0,
        unknown_create_hold_seconds: float = 900.0,
        external_writes_enabled: bool = False,
        settings_provider: Callable[[], object] | None = None,
    ) -> None:
        self.active_probe_enabled = bool(active_probe_enabled)
        self.external_writes_enabled = bool(external_writes_enabled)
        self._settings_provider = settings_provider
        self._active_probe_override: bool | None = None
        self._external_writes_override: bool | None = None
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

    def _current_write_flags(self) -> tuple[bool, bool]:
        active_enabled = self.active_probe_enabled
        writes_enabled = self.external_writes_enabled
        if self._settings_provider is not None:
            current = self._settings_provider()
            # Runtime settings are the global authority. A per-policy value
            # can only further restrict them; it can never override a global
            # false kill switch.
            active_enabled = active_enabled and bool(getattr(current, "wanda_active_probe_enabled", False))
            writes_enabled = writes_enabled and bool(getattr(current, "external_writes_enabled", False))
        if self._active_probe_override is not None:
            active_enabled = active_enabled and bool(self._active_probe_override)
        if self._external_writes_override is not None:
            writes_enabled = writes_enabled and bool(self._external_writes_override)
        # An explicitly supplied process-level false is a hard fuse even for
        # callers that construct a policy directly instead of via Settings.
        if _explicit_env_flag("WANDA_ACTIVE_PROBE_ENABLED") is False:
            active_enabled = False
        writes_env = _explicit_env_flag("EXTERNAL_WRITES_ENABLED")
        if writes_env is None:
            writes_env = _explicit_env_flag("WANDA_EXTERNAL_WRITES_ENABLED")
        if writes_env is False:
            writes_enabled = False
        return active_enabled, writes_enabled

    def ensure_enabled(self) -> None:
        """Check global and provider-write fuses before creating local Probe state."""
        active_enabled, writes_enabled = self._current_write_flags()
        if not active_enabled:
            raise ProbeDisabledError()
        if not writes_enabled:
            raise ProbeError("external_writes_disabled", "外部写操作总开关当前已关闭。")

    def ensure_allowed(self) -> None:
        """Admit a Probe only when every write-safety gate is currently true."""
        self.ensure_enabled()
        if not _ACTIVE_PROBE_CONTEXT.get():
            raise ProbeError("probe_flow_not_explicitly_allowed", "当前流程未显式允许 Active Probe。")
        if not _PROBE_LEASE_CONTEXT.get():
            raise ProbeError("probe_lease_required", "Active Probe 缺少账号租约。")
        if not _PROBE_RISK_CONTEXT.get():
            raise ProbeError("probe_risk_check_required", "Active Probe 未通过风险检查。")

    @staticmethod
    def context_allows_active_probe() -> bool:
        return _ACTIVE_PROBE_CONTEXT.get()

    @staticmethod
    def context_allows_probe_recovery() -> bool:
        return _PROBE_RECOVERY_CONTEXT.get()

    def ensure_cleanup_allowed(self) -> None:
        """Allow only durable cleanup while the normal Probe fuse is off."""
        if not self.context_allows_probe_recovery():
            self.ensure_allowed()


@contextmanager
def allow_active_probe(*, lease_acquired: bool = False, risk_approved: bool = False) -> Iterator[None]:
    """Explicitly mark a trusted Probe flow and its admission facts."""
    flow_token = _ACTIVE_PROBE_CONTEXT.set(True)
    lease_token = _PROBE_LEASE_CONTEXT.set(bool(lease_acquired))
    risk_token = _PROBE_RISK_CONTEXT.set(bool(risk_approved))
    try:
        yield
    finally:
        _PROBE_RISK_CONTEXT.reset(risk_token)
        _PROBE_LEASE_CONTEXT.reset(lease_token)
        _ACTIVE_PROBE_CONTEXT.reset(flow_token)


def _explicit_env_flag(name: str) -> bool | None:
    value = os.getenv(name)
    if value is None:
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


@contextmanager
def allow_probe_recovery() -> Iterator[None]:
    token = _PROBE_RECOVERY_CONTEXT.set(True)
    try:
        yield
    finally:
        _PROBE_RECOVERY_CONTEXT.reset(token)


@contextmanager
def disable_active_probe() -> Iterator[None]:
    flow_token = _ACTIVE_PROBE_CONTEXT.set(False)
    lease_token = _PROBE_LEASE_CONTEXT.set(False)
    risk_token = _PROBE_RISK_CONTEXT.set(False)
    try:
        yield
    finally:
        _PROBE_RISK_CONTEXT.reset(risk_token)
        _PROBE_LEASE_CONTEXT.reset(lease_token)
        _ACTIVE_PROBE_CONTEXT.reset(flow_token)
