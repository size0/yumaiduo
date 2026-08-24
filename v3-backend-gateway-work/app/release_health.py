"""Bounded, secret-safe runtime contract verification for release smoke tests."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

EXPECTED_V3_RUNTIME_CONTRACT: Final = "wanda-agent-runtime-v37-model-led-native-tools"
EXPECTED_AGENT_RUNTIME_VERSION: Final = "wanda-agent-runtime-v37-model-led-native-tools"
MAX_HEALTH_RESPONSE_BYTES: Final = 64 * 1024


def _report(*, ready: bool, code: str, v3_match: bool, plugin_match: bool, registered: bool) -> dict[str, object]:
    return {
        "ready": ready,
        "code": code,
        "v3_contract_match": v3_match,
        "plugin_contract_match": plugin_match,
        "plugin_registered": registered,
    }


def validate_runtime_health(v3_health: object, plugin_health: object) -> dict[str, object]:
    """Validate exact deployed contracts without echoing arbitrary health payload fields."""
    v3 = v3_health if isinstance(v3_health, Mapping) else {}
    plugin = plugin_health if isinstance(plugin_health, Mapping) else {}
    application_value = plugin.get("application")
    application = application_value if isinstance(application_value, Mapping) else {}
    v3_match = v3.get("runtime_contract") == EXPECTED_V3_RUNTIME_CONTRACT
    plugin_match = application.get("agent_runtime_version") == EXPECTED_AGENT_RUNTIME_VERSION
    registered = plugin.get("registered") is True
    if v3.get("status") != "ok":
        code = "v3_unhealthy"
    elif not v3_match:
        code = "v3_runtime_contract_mismatch"
    elif plugin.get("ok") is not True or application.get("ok") is False:
        code = "plugin_unhealthy"
    elif not registered:
        code = "plugin_not_registered"
    elif not plugin_match:
        code = "plugin_runtime_contract_mismatch"
    else:
        code = "ready"
    return _report(
        ready=code == "ready",
        code=code,
        v3_match=v3_match,
        plugin_match=plugin_match,
        registered=registered,
    )


def fetch_health_json(url: str, *, timeout_seconds: float = 5.0) -> dict[str, Any]:
    parsed = urlsplit(str(url))
    loopback_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if not (parsed.scheme == "https" or loopback_http) or parsed.username or parsed.password:
        raise ValueError("health URL must use HTTPS or loopback HTTP")
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - URL is validated above.
        body = response.read(MAX_HEALTH_RESPONSE_BYTES + 1)
    if len(body) > MAX_HEALTH_RESPONSE_BYTES:
        raise ValueError("health response is too large")
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("health response must be an object")
    return payload
