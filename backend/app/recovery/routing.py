from __future__ import annotations

from typing import Any


def select_runtime(*, recovery_enabled: bool, legacy_enabled: bool, image_event: bool, text_event: bool,
                   recovery_runtime: Any | None, legacy_runtime: Any | None) -> Any | None:
    """Select exactly one quote runtime for a message event."""
    if recovery_enabled and (image_event or text_event):
        return recovery_runtime
    if legacy_enabled and image_event:
        return legacy_runtime
    return None
