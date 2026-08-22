"""Secret-safe deployment preflight for the direct Wanda gateway."""
from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

from .wanda_direct_gateway import DirectGatewayError
from .wanda_official_api import DEFAULT_ACCOUNT_POOL_PATH, JsonWandaAccountSource

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _report(*, ready: bool, enabled: bool, code: str, eligible_account_count: int = 0) -> dict[str, object]:
    return {
        "ready": ready,
        "enabled": enabled,
        "code": code,
        "eligible_account_count": eligible_account_count,
    }


async def validate_direct_gateway_environment(
    environment: Mapping[str, str] | None = None,
    *,
    require_enabled: bool = False,
) -> dict[str, object]:
    """Validate direct-gateway prerequisites without returning paths or secrets."""
    env = environment if environment is not None else os.environ
    enabled = str(env.get("WANDA_DIRECT_GATEWAY_ENABLED", "false")).strip().lower() in _TRUE_VALUES
    if not enabled:
        return _report(
            ready=not require_enabled,
            enabled=False,
            code="direct_gateway_required" if require_enabled else "disabled",
        )

    reference_key = str(env.get("WANDA_PRICING_ACCOUNT_REF_KEY", "")).strip()
    if len(reference_key.encode("utf-8")) < 32:
        return _report(ready=False, enabled=True, code="pricing_ref_key_invalid")

    configured_path = str(env.get("WANDA_DIRECT_ACCOUNT_POOL_PATH", "")).strip()
    pool_path = Path(configured_path) if configured_path else DEFAULT_ACCOUNT_POOL_PATH
    try:
        file_stat = pool_path.stat()
    except OSError:
        return _report(ready=False, enabled=True, code="account_pool_unavailable")
    if not stat.S_ISREG(file_stat.st_mode):
        return _report(ready=False, enabled=True, code="account_pool_unavailable")
    if os.name != "nt" and file_stat.st_mode & 0o027:
        return _report(ready=False, enabled=True, code="account_pool_permissions_unsafe")

    try:
        accounts = await JsonWandaAccountSource(pool_path).list_accounts()
    except DirectGatewayError as error:
        code = error.code if error.code in {"account_pool_unavailable", "account_pool_invalid"} else "account_pool_unavailable"
        return _report(ready=False, enabled=True, code=code)
    if not accounts:
        return _report(ready=False, enabled=True, code="eligible_account_unavailable")
    return _report(ready=True, enabled=True, code="ready", eligible_account_count=len(accounts))
