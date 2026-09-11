from __future__ import annotations


class ProbeError(RuntimeError):
    """Safe structured error; never carries provider credentials or raw payloads."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = str(code).strip() or "probe_failed"
        self.message = message or self.code
        super().__init__(self.code)


class ProbeDisabledError(ProbeError):
    def __init__(self, code: str = "active_probe_disabled") -> None:
        super().__init__(code, "Active Probe 当前已关闭。")


CREATE_UNKNOWN_UNRECOVERABLE_AUTOMATICALLY = "CREATE_UNKNOWN_UNRECOVERABLE_AUTOMATICALLY"


class ProbeCleanupError(ProbeError):
    def __init__(self) -> None:
        super().__init__("temporary_lock_release_unverified", "临时锁座释放尚未确认。")
