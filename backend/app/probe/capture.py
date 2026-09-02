from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .models import ProbeResult


_CAPTURE_FILES = (
    "create_order.response.json",
    "lock_status.response.json",
    "activity_offers.response.json",
    "cancel.response.json",
    "cancel_status.response.json",
    "seat_release_0s.response.json",
    "seat_release_2s.response.json",
    "seat_release_5s.response.json",
    "expected_probe_result.json",
)
_SENSITIVE_KEYS = {
    "token", "access_token", "refresh_token", "cookie", "cookies", "authorization",
    "csrf", "csrf_token", "app_secret", "appsecret", "device_id", "deviceid",
    "signature", "sign", "phone", "mobile", "mobilephone", "手机号", "设备id",
}
_ORDER_KEYS = {"orderid", "order_id", "temporary_order_id", "temporary_order_reference"}
_PHONE_RE = re.compile(r"(?<!\d)1\d{10}(?!\d)")


def redact_capture(value: object) -> object:
    """Remove credentials and replace temporary order IDs without retaining raw V3 payloads."""
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if _is_sensitive_key(normalized):
                continue
            if normalized in _ORDER_KEYS:
                result[str(key)] = "fixture-order-1"
            else:
                result[str(key)] = redact_capture(item)
        return result
    if isinstance(value, list):
        return [redact_capture(item) for item in value[:100]]
    if isinstance(value, tuple):
        return [redact_capture(item) for item in value[:100]]
    if isinstance(value, str):
        return _PHONE_RE.sub("<REDACTED_PHONE>", value)
    return value


def assert_capture_redacted(value: object) -> None:
    """Fail closed if a capture still contains a sensitive key or phone number."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if _is_sensitive_key(normalized):
                raise ValueError("capture_sensitive_field_present")
            assert_capture_redacted(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_capture_redacted(item)
    elif isinstance(value, str) and _PHONE_RE.search(value):
        raise ValueError("capture_phone_present")


def _is_sensitive_key(normalized: str) -> bool:
    normalized_sensitive = {item.replace("_", "") for item in _SENSITIVE_KEYS}
    return normalized in _SENSITIVE_KEYS or normalized.replace("_", "") in normalized_sensitive


def write_v3_capture(
    directory: Path,
    *,
    manifest: Mapping[str, object],
    responses: Mapping[str, object],
    expected_probe_result: Mapping[str, object],
) -> Path:
    """Write a versioned sanitized capture; callers provide already collected offline data."""
    required = {"fixture_version", "provider", "cinema_id", "show_id", "seat_type", "capture_schema_version", "source"}
    if not required.issubset(manifest) or manifest.get("source") != "V3":
        raise ValueError("capture_manifest_incomplete")
    sanitized_manifest = {
        **dict(manifest),
        "captured_at": str(manifest.get("captured_at") or datetime.now(timezone.utc).isoformat()),
        "sensitive_data_removed": True,
    }
    safe_responses = {name: redact_capture(responses.get(name, {})) for name in _CAPTURE_FILES[:-1]}
    safe_expected = redact_capture(expected_probe_result)
    ProbeResult.model_validate(safe_expected)
    assert_capture_redacted(sanitized_manifest)
    assert_capture_redacted(safe_responses)
    assert_capture_redacted(safe_expected)
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    (target / "manifest.json").write_text(json.dumps(sanitized_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, value in safe_responses.items():
        (target / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    (target / "expected_probe_result.json").write_text(json.dumps(safe_expected, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
