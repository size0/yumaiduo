from __future__ import annotations

from typing import Any


class PricingError(ValueError):
    """Structured deterministic error raised by the V4 pricing boundary."""

    def __init__(self, code: str, message: str | None = None, **details: Any) -> None:
        self.code = code
        self.details = details
        super().__init__(message or code)
