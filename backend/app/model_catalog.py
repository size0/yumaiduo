from __future__ import annotations

from time import perf_counter
from typing import Any

import httpx

from .diagnostics import DiagnosticsStore
from .errors import ProviderError
from .models import ModelCatalogResponse
from .observability import LOGGER


class ModelCatalogService:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        diagnostics: DiagnosticsStore | None = None,
    ) -> None:
        self._client = client
        self._diagnostics = diagnostics or DiagnosticsStore()

    async def list_models(self, base_url: str, api_key: str) -> ModelCatalogResponse:
        url = self._models_url(base_url)
        started_at = perf_counter()
        LOGGER.info("event=model_catalog_started host=%s", httpx.URL(url).host)
        try:
            if self._client is not None:
                response = await self._client.get(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=20,
                )
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.get(
                        url,
                        headers={"Authorization": f"Bearer {api_key}"},
                        timeout=20,
                    )
        except httpx.TimeoutException as error:
            raise ProviderError("model_catalog_timeout", "获取模型超时，请稍后重试。") from error
        except httpx.HTTPError as error:
            raise ProviderError("model_catalog_unavailable", "暂时无法连接模型服务。") from error

        duration_ms = round((perf_counter() - started_at) * 1000, 1)
        try:
            body: Any = response.json()
        except ValueError as error:
            raise ProviderError("model_catalog_response_invalid", "模型列表返回的不是有效 JSON。") from error

        self._diagnostics.add(
            "model_catalog_response",
            status=response.status_code,
            duration_ms=duration_ms,
            provider_request_id=response.headers.get("x-request-id") or response.headers.get("request-id"),
            response=body,
        )
        LOGGER.info(
            "event=model_catalog_completed status=%d duration_ms=%.1f",
            response.status_code,
            duration_ms,
        )
        if response.status_code in {401, 403}:
            raise ProviderError("model_catalog_authentication_failed", "获取模型认证失败，请检查中转站 API Key。")
        if response.status_code == 429:
            raise ProviderError("model_catalog_rate_limited", "获取模型请求过多，请稍后再试。")
        if response.status_code >= 400:
            raise ProviderError("model_catalog_request_rejected", "模型服务拒绝了获取模型请求。")

        raw_models = body.get("data") if isinstance(body, dict) else body
        if not isinstance(raw_models, list):
            raise ProviderError("model_catalog_response_invalid", "模型列表响应缺少 data 数组。")
        models = sorted({
            item["id"].strip()
            for item in raw_models
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and item["id"].strip()
            and len(item["id"].strip()) <= 200
        }, key=str.casefold)
        if not models:
            raise ProviderError("model_catalog_response_invalid", "模型列表中没有可用的模型 ID。")
        return ModelCatalogResponse(models=models[:1000], count=min(len(models), 1000))

    @staticmethod
    def _models_url(base_url: str) -> str:
        normalized = base_url.rstrip("/")
        if not normalized.endswith("/v1"):
            normalized += "/v1"
        return normalized + "/models"
