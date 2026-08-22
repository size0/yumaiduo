from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.direct_gateway_preflight import validate_direct_gateway_environment


ACCOUNT = {
    "account_id": "account-1",
    "phone": "13800000000",
    "status": "online",
    "risk_status": "normal",
    "is_wplus": True,
    "token": "secret-token",
    "remaining": None,
}


def validate(env: dict[str, str], *, require_enabled: bool = True):
    return asyncio.run(validate_direct_gateway_environment(env, require_enabled=require_enabled))


def test_preflight_reports_only_bounded_non_secret_readiness(tmp_path: Path) -> None:
    pool = tmp_path / "accounts.json"
    pool.write_text(json.dumps([ACCOUNT]), encoding="utf-8")
    pool.chmod(0o640)
    report = validate({
        "WANDA_DIRECT_GATEWAY_ENABLED": "true",
        "WANDA_DIRECT_ACCOUNT_POOL_PATH": str(pool),
        "WANDA_PRICING_ACCOUNT_REF_KEY": "production-reference-key-at-least-32",
    })
    assert report == {
        "ready": True,
        "enabled": True,
        "code": "ready",
        "eligible_account_count": 1,
    }
    rendered = json.dumps(report)
    assert "secret-token" not in rendered
    assert "production-reference-key" not in rendered
    assert str(pool) not in rendered


def test_preflight_fails_closed_for_missing_key_pool_or_eligible_account(tmp_path: Path) -> None:
    missing_pool = validate({
        "WANDA_DIRECT_GATEWAY_ENABLED": "true",
        "WANDA_DIRECT_ACCOUNT_POOL_PATH": str(tmp_path / "missing.json"),
        "WANDA_PRICING_ACCOUNT_REF_KEY": "production-reference-key-at-least-32",
    })
    assert missing_pool["code"] == "account_pool_unavailable"

    pool = tmp_path / "accounts.json"
    pool.write_text(json.dumps([{**ACCOUNT, "remaining": 0}]), encoding="utf-8")
    pool.chmod(0o640)
    no_account = validate({
        "WANDA_DIRECT_GATEWAY_ENABLED": "true",
        "WANDA_DIRECT_ACCOUNT_POOL_PATH": str(pool),
        "WANDA_PRICING_ACCOUNT_REF_KEY": "production-reference-key-at-least-32",
    })
    assert no_account["code"] == "eligible_account_unavailable"

    missing_key = validate({
        "WANDA_DIRECT_GATEWAY_ENABLED": "true",
        "WANDA_DIRECT_ACCOUNT_POOL_PATH": str(pool),
    })
    assert missing_key["code"] == "pricing_ref_key_invalid"


def test_preflight_distinguishes_intentionally_disabled_from_required_direct_mode() -> None:
    disabled = validate({}, require_enabled=False)
    assert disabled == {
        "ready": True,
        "enabled": False,
        "code": "disabled",
        "eligible_account_count": 0,
    }
    required = validate({}, require_enabled=True)
    assert required["ready"] is False
    assert required["code"] == "direct_gateway_required"
