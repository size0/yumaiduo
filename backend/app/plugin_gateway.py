from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response


PUBLIC_GATEWAY_SUFFIXES = (
    ".css",
    ".gif",
    ".html",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".map",
    ".png",
    ".svg",
    ".webp",
)


@dataclass(frozen=True)
class PluginGatewayRegistration:
    plugin_id: str
    manifest: dict[str, Any]
    base_url: str
    plugin_token: str
    webhook_secret: str
    registered_at: str
    updated_at: str
    revision: int


@dataclass(frozen=True)
class PluginGatewaySession:
    token: str
    plugin_id: str
    tenant_id: str
    user_id: str
    issued_at: str
    expires_at: str
    gateway_base: str


class PluginGatewayStore:
    def __init__(self, path: str | Path, *, session_ttl_seconds: int = 300) -> None:
        self._path = Path(path)
        self._session_ttl_seconds = session_ttl_seconds
        self._plugins: dict[str, dict[str, Any]] = {}
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = RLock()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        plugins = payload.get("plugins") if isinstance(payload, dict) else None
        if isinstance(plugins, dict):
            self._plugins = {
                str(plugin_id): dict(value)
                for plugin_id, value in plugins.items()
                if isinstance(value, dict)
            }

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"plugins": self._plugins}, ensure_ascii=False, indent=2, sort_keys=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        tmp_path.replace(self._path)

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        normalized = str(base_url or "").strip()
        if not normalized:
            raise ValueError("plugin_base_url_required")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("plugin_base_url_invalid")
        return normalized.rstrip("/")

    def register(self, manifest: Mapping[str, Any], *, base_url: str) -> dict[str, Any]:
        plugin_id = str(manifest.get("id") or "").strip()
        if not plugin_id:
            raise ValueError("plugin_id_required")
        normalized_base_url = self._normalize_base_url(base_url)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            existing = self._plugins.get(plugin_id)
            revision = int(existing.get("revision") or 0) + 1 if existing else 1
            record: dict[str, Any] = {
                "plugin_id": plugin_id,
                "manifest": dict(manifest),
                "base_url": normalized_base_url,
                "plugin_token": f"yp_{secrets.token_urlsafe(32)}",
                "webhook_secret": secrets.token_hex(32),
                "registered_at": existing.get("registered_at") if existing else now,
                "updated_at": now,
                "revision": revision,
            }
            if existing is None:
                record["registered_at"] = now
            self._plugins[plugin_id] = record
            self._save()
            return dict(record)

    def get(self, plugin_id: str) -> dict[str, Any] | None:
        normalized = str(plugin_id or "").strip()
        if not normalized:
            return None
        with self._lock:
            record = self._plugins.get(normalized)
            return dict(record) if record is not None else None

    def issue_session(
        self,
        plugin_id: str,
        tenant_id: str,
        user_id: str,
        *,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        registration = self.get(plugin_id)
        if registration is None:
            raise KeyError("plugin_not_registered")
        normalized_tenant = str(tenant_id or "").strip()
        normalized_user = str(user_id or "").strip()
        if not normalized_tenant:
            raise ValueError("tenant_id_required")
        if not normalized_user:
            raise ValueError("user_id_required")
        ttl = int(ttl_seconds or self._session_ttl_seconds)
        if ttl < 1:
            raise ValueError("session_ttl_invalid")
        now = datetime.now(timezone.utc)
        token = f"yps_{secrets.token_urlsafe(32)}"
        session = {
            "token": token,
            "plugin_id": plugin_id,
            "tenant_id": normalized_tenant,
            "user_id": normalized_user,
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
            "gateway_base": f"/api/v1/plugin/{plugin_id}/gateway",
        }
        with self._lock:
            self._purge_expired_locked(now)
            self._sessions[token] = session
        return dict(session)

    def _purge_expired_locked(self, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        expired = [token for token, session in self._sessions.items() if self._session_expired(session, current)]
        for token in expired:
            self._sessions.pop(token, None)

    @staticmethod
    def _session_expired(session: Mapping[str, Any], now: datetime) -> bool:
        expires_at = str(session.get("expires_at") or "").strip()
        if not expires_at:
            return True
        try:
            parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed <= now

    def validate_session(self, token: str, *, plugin_id: str) -> tuple[dict[str, Any] | None, str | None]:
        normalized = str(token or "").strip()
        if not normalized:
            return None, "missing"
        now = datetime.now(timezone.utc)
        with self._lock:
            session = self._sessions.get(normalized)
            if session is None:
                return None, "missing"
            if self._session_expired(session, now):
                self._sessions.pop(normalized, None)
                return None, "expired"
            if str(session.get("plugin_id") or "").strip() != str(plugin_id or "").strip():
                return None, "plugin_mismatch"
            self._purge_expired_locked(now)
            return dict(session), None


def build_gateway_signature(secret: str, timestamp: str, raw_body: str) -> str:
    ts = str(timestamp).strip()
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{ts}.{raw_body}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return expected


def verify_gateway_signature(secret: str, timestamp: str, signature: str, raw_body: str) -> bool:
    expected = build_gateway_signature(secret, timestamp, raw_body)
    return len(expected) == len(signature) and hmac.compare_digest(expected, signature)


def build_proxy_url(base_url: str, path: str, query: str = "") -> str:
    target = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    return f"{target}?{query}" if query else target


def is_public_ui_request(path: str, method: str) -> bool:
    if method.upper() not in {"GET", "HEAD"}:
        return False
    normalized = str(path or "").lstrip("/")
    if normalized in {"ui", "ui/"}:
        return True
    if not normalized.startswith("ui/"):
        return False
    if normalized.startswith(("ui/api/", "ui/v4/")):
        return False
    return normalized.endswith(PUBLIC_GATEWAY_SUFFIXES)


async def _forward_plugin_gateway(
    request: Request,
    *,
    plugin_id: str,
    gateway_path: str,
    registration: Mapping[str, Any],
    session: Mapping[str, Any] | None,
    session_token: str | None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Response:
    raw_body = b""
    if request.method.upper() not in {"GET", "HEAD"}:
        raw_body = await request.body()
    raw_text = raw_body.decode("utf-8") if raw_body else ""
    timestamp = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    signature = build_gateway_signature(str(registration["webhook_secret"]), timestamp, raw_text)
    outbound_headers = {}
    for key, value in request.headers.items():
        lowered = key.lower()
        if lowered in {
            "authorization",
            "connection",
            "content-length",
            "cookie",
            "host",
            "proxy-authorization",
            "te",
            "trailer",
            "transfer-encoding",
            "upgrade",
            "x-plugin-session",
        }:
            continue
        if lowered.startswith("x-yumaiduo-"):
            continue
        outbound_headers[key] = value
    outbound_headers["x-yumaiduo-plugin-id"] = plugin_id
    outbound_headers["x-yumaiduo-timestamp"] = timestamp
    outbound_headers["x-yumaiduo-signature"] = signature
    if session is not None:
        outbound_headers["x-yumaiduo-tenant-id"] = str(session["tenant_id"])
        outbound_headers["x-yumaiduo-user-id"] = str(session["user_id"])
    if session_token:
        outbound_headers["x-plugin-session"] = session_token
    if request.headers.get("content-type"):
        outbound_headers["content-type"] = request.headers["content-type"]

    target_url = build_proxy_url(str(registration["base_url"]), "/" + gateway_path, request.url.query)
    client_kwargs: dict[str, Any] = {"timeout": httpx.Timeout(15.0), "follow_redirects": False}
    if transport is not None:
        client_kwargs["transport"] = transport
    async with httpx.AsyncClient(**client_kwargs) as client:
        upstream = await client.request(
            request.method,
            target_url,
            headers=outbound_headers,
            content=raw_body if request.method.upper() not in {"GET", "HEAD"} else None,
        )
    headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in {"content-length", "connection", "keep-alive", "transfer-encoding", "upgrade"}
    }
    return Response(
        content=upstream.content if request.method.upper() != "HEAD" else b"",
        status_code=upstream.status_code,
        headers=headers,
        media_type=upstream.headers.get("content-type"),
    )


def create_plugin_gateway_router(
    store: PluginGatewayStore,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> APIRouter:
    router = APIRouter()

    def require_developer_token(value: str | None) -> None:
        expected = os.getenv("WANDA_PLUGIN_DEVELOPER_TOKEN", "").strip()
        if not expected:
            raise HTTPException(status_code=503, detail="plugin_registry_not_configured")
        if not value or not hmac.compare_digest(value, expected):
            raise HTTPException(status_code=401, detail="plugin_registry_unauthorized")

    def read_identity(
        body: Mapping[str, Any],
        *,
        tenant_header: str | None,
        user_header: str | None,
    ) -> tuple[str, str]:
        body_tenant = str(body.get("tenant_id") or body.get("tenantId") or "").strip()
        body_user = str(body.get("user_id") or body.get("userId") or "").strip()
        tenant_id = str(tenant_header or body_tenant or "").strip()
        user_id = str(user_header or body_user or "").strip()
        if not tenant_id:
            raise HTTPException(status_code=401, detail="plugin_console_tenant_required")
        if not user_id:
            raise HTTPException(status_code=401, detail="plugin_console_user_required")
        if body_tenant and tenant_header and body_tenant != tenant_header:
            raise HTTPException(status_code=403, detail="plugin_console_tenant_forbidden")
        if body_user and user_header and body_user != user_header:
            raise HTTPException(status_code=403, detail="plugin_console_user_forbidden")
        return tenant_id, user_id

    @router.post("/api/v1/plugin/runtime/register")
    async def register_plugin(
        request: Request,
        body: dict[str, object],
        x_plugin_developer_token: str | None = Header(default=None, alias="X-Plugin-Developer-Token"),
    ) -> dict[str, object]:
        require_developer_token(x_plugin_developer_token)
        manifest = body.get("manifest") if isinstance(body.get("manifest"), Mapping) else None
        base_url = str(body.get("baseUrl") or body.get("base_url") or "").strip()
        if manifest is None:
            raise HTTPException(status_code=422, detail="plugin_manifest_required")
        try:
            registration = store.register(manifest, base_url=base_url)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {
            "ok": True,
            "data": {
                "pluginId": registration["plugin_id"],
                "token": registration["plugin_token"],
                "webhookSecret": registration["webhook_secret"],
                "baseUrl": registration["base_url"],
                "registeredAt": registration["registered_at"],
                "revision": registration["revision"],
            },
        }

    @router.post("/api/v1/plugin/{plugin_id}/gateway/session")
    async def create_session(
        plugin_id: str,
        body: dict[str, object],
        x_wanda_tenant_id: str | None = Header(default=None, alias="X-Wanda-Tenant-Id"),
        x_yumaiduo_tenant_id: str | None = Header(default=None, alias="X-Yumaiduo-Tenant-Id"),
        x_wanda_user_id: str | None = Header(default=None, alias="X-Wanda-User-Id"),
        x_yumaiduo_user_id: str | None = Header(default=None, alias="X-Yumaiduo-User-Id"),
    ) -> dict[str, object]:
        registration = store.get(plugin_id)
        if registration is None:
            raise HTTPException(status_code=404, detail="plugin_not_found")
        tenant_id, user_id = read_identity(
            body,
            tenant_header=x_wanda_tenant_id or x_yumaiduo_tenant_id,
            user_header=x_wanda_user_id or x_yumaiduo_user_id,
        )
        try:
            ttl_seconds = int(body.get("ttl_seconds") or body.get("ttlSeconds") or 0) or None
            session = store.issue_session(plugin_id, tenant_id, user_id, ttl_seconds=ttl_seconds)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except KeyError:
            raise HTTPException(status_code=404, detail="plugin_not_found") from None
        return {
            "ok": True,
            "data": {
                "token": session["token"],
                "expiresAt": session["expires_at"],
                "gatewayBase": session["gateway_base"],
                "pluginId": session["plugin_id"],
                "tenantId": session["tenant_id"],
                "userId": session["user_id"],
            },
        }

    @router.api_route("/api/v1/plugin/{plugin_id}/gateway", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def gateway_root(request: Request, plugin_id: str) -> Response:
        return await gateway_proxy(request, plugin_id=plugin_id, gateway_path="ui")

    @router.api_route("/api/v1/plugin/{plugin_id}/gateway/{gateway_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def gateway_proxy(request: Request, plugin_id: str, gateway_path: str) -> Response:
        registration = store.get(plugin_id)
        if registration is None:
            raise HTTPException(status_code=404, detail="plugin_not_found")
        normalized_path = str(gateway_path or "ui").lstrip("/") or "ui"
        session_token = str(request.headers.get("x-plugin-session") or "").strip()
        session: dict[str, Any] | None = None
        if not is_public_ui_request(normalized_path, request.method):
            session, reason = store.validate_session(session_token, plugin_id=plugin_id)
            if reason == "missing":
                raise HTTPException(status_code=401, detail="plugin_session_required")
            if reason == "expired":
                raise HTTPException(status_code=401, detail="plugin_session_expired")
            if reason == "plugin_mismatch":
                raise HTTPException(status_code=403, detail="plugin_session_forbidden")
        return await _forward_plugin_gateway(
            request,
            plugin_id=plugin_id,
            gateway_path=normalized_path,
            registration=registration,
            session=session,
            session_token=session_token if session is not None else None,
            transport=transport,
        )

    return router
