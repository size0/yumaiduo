"""开放平台应用凭据（AppKey / AppSecret）与请求签名校验。

比桥密钥多一层「应用身份」：每个接入方一对 ``AppKey`` / ``AppSecret``，
请求带 ``X-App-Key`` / ``X-Timestamp`` / ``X-Nonce`` / ``X-Sign``，
签名算在**整个请求体**上，并做时间窗 + nonce 防重放。

与桥密钥（``bridge_keys.py``）**并存**：老接入方继续用 ``X-Plugin-Bridge-Key``，
新接入方用应用签名；两条路都在中间件的白名单路径上生效，便于灰度迁移。

签名规范（与文档一致）::

    canonical_body = 请求体原始字节；GET 或空体时用 "{}"
    待签名串        = appKey + timestamp + nonce + canonical_body
    X-Sign         = hex(HMAC-SHA256(待签名串, appSecret))

``timestamp`` 是 **10 位秒级**；与服务端偏差超过 ``_TIMESTAMP_WINDOW_SECONDS`` 拒绝；
``nonce`` 在窗口内只能使用一次（复用返回 nonce_reused，调用方应换新 nonce 重试）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets as _secrets
import time
import uuid
from typing import Any, Optional

from .store import DATA_DIR

APP_CREDENTIALS_FILE = DATA_DIR / "app_credentials.json"
APP_NONCES_FILE = DATA_DIR / "app_signature_nonces.json"

_TIMESTAMP_WINDOW_SECONDS = 300
_NONCE_TTL_SECONDS = 300

REASON_OK = "ok"
REASON_UNKNOWN_APP = "unknown_app_key"
REASON_DISABLED_APP = "disabled_app_key"
REASON_EXPIRED = "timestamp_expired"
REASON_BAD_SIGNATURE = "bad_signature"
REASON_NONCE_REUSED = "nonce_reused"
REASON_MISSING = "missing_signature_fields"


# ── 凭据存储 ──────────────────────────────────────────────


def _read() -> list[dict[str, Any]]:
    try:
        raw = APP_CREDENTIALS_FILE.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    items = data.get("credentials") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    return [
        item
        for item in items
        if isinstance(item, dict) and str(item.get("app_key") or "") and str(item.get("app_secret") or "")
    ]


def _write(items: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"credentials": items}, ensure_ascii=False, indent=2)
    tmp = APP_CREDENTIALS_FILE.with_name(APP_CREDENTIALS_FILE.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(APP_CREDENTIALS_FILE)
    try:
        APP_CREDENTIALS_FILE.chmod(0o600)
    except OSError:
        pass


def _mask(secret: str) -> str:
    text = str(secret or "")
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}{'*' * 12}{text[-4:]}"


def _view(entry: dict[str, Any]) -> dict[str, Any]:
    secret = str(entry.get("app_secret") or "")
    return {
        "id": str(entry.get("id") or ""),
        "name": str(entry.get("name") or "未命名"),
        "app_key": str(entry.get("app_key") or ""),
        "app_secret": secret,
        "masked_secret": _mask(secret),
        "created_at": str(entry.get("created_at") or ""),
        "disabled": bool(entry.get("disabled")),
        "tenant_id": str(entry.get("tenant_id") or ""),
    }


def list_credentials() -> list[dict[str, Any]]:
    """后台设置用列表（含明文 secret —— 只允许管理员会话调用）。"""
    return [_view(item) for item in _read()]


def create_credential(name: str = "", tenant_id: str = "") -> dict[str, Any]:
    entry = {
        "id": uuid.uuid4().hex[:12],
        "name": (str(name or "").strip() or f"应用 {time.strftime('%Y-%m-%d %H:%M')}")[:64],
        "app_key": "ak_" + _secrets.token_hex(12),
        "app_secret": "sk_" + _secrets.token_urlsafe(32),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "disabled": False,
        "tenant_id": str(tenant_id or "").strip(),
    }
    items = _read()
    items.append(entry)
    _write(items)
    return _view(entry)


def set_disabled(credential_id: str, disabled: bool) -> Optional[dict[str, Any]]:
    target = str(credential_id or "")
    if not target:
        return None
    items = _read()
    found = None
    for item in items:
        if str(item.get("id")) == target:
            item["disabled"] = bool(disabled)
            found = item
            break
    if found is None:
        return None
    _write(items)
    return _view(found)


def set_tenant(credential_id: str, tenant_id: str) -> Optional[dict[str, Any]]:
    """Bind a credential to one tenant for order integration calls."""
    target = str(credential_id or "")
    if not target:
        return None
    items = _read()
    for item in items:
        if str(item.get("id")) == target:
            item["tenant_id"] = str(tenant_id or "").strip()
            _write(items)
            return _view(item)
    return None


def delete(credential_id: str) -> bool:
    target = str(credential_id or "")
    if not target:
        return False
    items = _read()
    kept = [item for item in items if str(item.get("id")) != target]
    if len(kept) == len(items):
        return False
    _write(kept)
    return True


def find(app_key: str) -> Optional[dict[str, Any]]:
    target = str(app_key or "").strip()
    if not target:
        return None
    for item in _read():
        if str(item.get("app_key")) == target:
            return item
    return None


# ── 签名 ────────────────────────────────────────────────


def signature_of(app_key: str, timestamp: str, nonce: str, body: bytes | str = b"{}") -> str:
    """对外暴露的签名规则说明用入口（需要 secret 时请用 :func:`sign`）。"""
    payload = body if isinstance(body, bytes) else str(body or "{}").encode("utf-8")
    if not payload:
        payload = b"{}"
    return f"{app_key}{timestamp}{nonce}".encode("utf-8") + payload


def sign(app_secret: str, app_key: str, timestamp: str, nonce: str, body: bytes | str = b"{}") -> str:
    """正确的签名实现：HMAC-SHA256(appKey+timestamp+nonce+body, appSecret)。"""
    message = signature_of(app_key, timestamp, nonce, body)
    return hmac.new(app_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


# ── nonce 防重放（文件 + TTL，自包含，不依赖订单存储）────────


def _claim_nonce(nonce: str, *, now: Optional[int] = None, ttl: int = _NONCE_TTL_SECONDS) -> bool:
    """返回 True 表示这个 nonce 可用（首次出现）。"""
    key = str(nonce or "").strip()
    if not key:
        return False
    moment = int(now if now is not None else time.time())
    try:
        raw = APP_NONCES_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        seen = {str(k): int(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        seen = {}
    seen = {k: v for k, v in seen.items() if v > moment}  # 清理过期
    if key in seen:
        _write_nonces(seen)
        return False
    seen[key] = moment + max(30, int(ttl))
    _write_nonces(seen)
    return True


def _write_nonces(seen: dict[str, int]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = APP_NONCES_FILE.with_name(APP_NONCES_FILE.name + ".tmp")
    tmp.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
    tmp.replace(APP_NONCES_FILE)


def verify_request(
    app_key: str,
    timestamp: str,
    nonce: str,
    signature: str,
    body: bytes | str = b"{}",
    *,
    now: Optional[int] = None,
    claim_nonce: bool = True,
) -> str:
    """校验一次带签名的请求。返回 ``ok`` 或具体原因（见模块顶部常量）。

    调用方据原因决定状态码：``nonce_reused`` → 409，其余鉴权失败 → 401。
    """
    if not app_key or not timestamp or not nonce or not signature:
        return REASON_MISSING
    entry = find(app_key)
    if entry is None:
        return REASON_UNKNOWN_APP
    if entry.get("disabled"):
        return REASON_DISABLED_APP
    try:
        ts = int(str(timestamp).strip())
    except (TypeError, ValueError):
        return REASON_EXPIRED
    moment = int(now if now is not None else time.time())
    if abs(moment - ts) > _TIMESTAMP_WINDOW_SECONDS:
        return REASON_EXPIRED
    expected = sign(str(entry.get("app_secret")), app_key, str(timestamp), nonce, body)
    if not hmac.compare_digest(str(signature).strip().lower(), expected):
        return REASON_BAD_SIGNATURE
    # 签名有效后再消费 nonce：否则攻击者可用错签名把别人的 nonce 提前占掉
    if claim_nonce and not _claim_nonce(nonce, now=moment):
        return REASON_NONCE_REUSED
    return REASON_OK
