from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .canonical import (
    ActivityOffersResult,
    CancelResult,
    CreateOrderResult,
    OrderStatusResult,
    SeatAvailabilityResult,
    canonical_activity,
    canonical_create_order,
    canonical_order_status,
)
from .errors import ProbeError
from .account_pool import ProbeAccount
from .policy import ProbePolicy
from ..config import Settings


OfflineTransport = Callable[[str, Mapping[str, object]], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]


class OfficialWandaProbeProvider:
    """Future official adapter contract; only an injected mock transport is usable in M2.5."""

    fixture = False

    def __init__(
        self,
        *,
        offline_transport: OfflineTransport | None = None,
        policy: ProbePolicy | None = None,
    ) -> None:
        self._offline_transport = offline_transport
        self._policy = policy

    def _ensure_write_allowed(self) -> None:
        policy = self._policy or ProbePolicy.from_settings(Settings.from_env)
        policy.ensure_allowed()

    async def _request(self, operation: str, payload: Mapping[str, object]) -> Mapping[str, Any]:
        if self._offline_transport is None:
            raise ProbeError("real_provider_disabled", "M2.5 禁止真实 Wanda HTTP。")
        result = self._offline_transport(operation, payload)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, Mapping):
            raise ProbeError("official_response_invalid")
        return result

    async def create_probe_order(self, *, account: ProbeAccount, show_id: str, seat_ids: list[str]) -> CreateOrderResult:
        self._ensure_write_allowed()
        payload = await self._request("create_probe_order", {"account_ref": account.account_ref, "show_id": show_id, "seat_ids": seat_ids})
        return canonical_create_order(payload)

    async def get_order_status(self, *, temporary_order_reference: str) -> OrderStatusResult:
        return canonical_order_status(await self._request("get_order_status", {"temporary_order_reference": temporary_order_reference}))

    async def get_activity_offers(self, *, temporary_order_reference: str) -> ActivityOffersResult:
        return canonical_activity(await self._request("get_activity_offers", {"temporary_order_reference": temporary_order_reference}))

    async def cancel_probe_order(self, *, temporary_order_reference: str) -> CancelResult:
        self._ensure_write_allowed()
        payload = await self._request("cancel_probe_order", {"temporary_order_reference": temporary_order_reference})
        return CancelResult(accepted=payload.get("code") in (0, "0") or payload.get("ok") is True)

    async def get_available_seats(self, *, show_id: str, seat_ids: list[str]) -> SeatAvailabilityResult:
        payload = await self._request("get_available_seats", {"show_id": show_id, "seat_ids": seat_ids})
        ids = payload.get("available_seat_ids") or payload.get("availableSeatIds") or []
        return SeatAvailabilityResult(available_seat_ids={str(item) for item in ids} if isinstance(ids, list) else set())
