"""Safe orchestration for direct Wanda temporary-order price probes.

The module is transport-agnostic: a separately reviewed official client owns
Wanda signing and response normalization.  This coordinator never calls the
legacy ticket HTTP gateway and never exposes account credentials in results,
errors, or logs.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from threading import Lock
from typing import Any, Protocol
from uuid import uuid4


class DirectGatewayError(RuntimeError):
    def __init__(self, code: str, message: str | None = None, *, retryable_before_create: bool = False) -> None:
        self.code = str(code).strip() or "wanda_direct_gateway_failed"
        self.retryable_before_create = retryable_before_create
        super().__init__(message or self.code)


@dataclass(frozen=True)
class AccountLease:
    account_id: str
    lease_id: str
    expires_at: float


class _MonotonicClock(Protocol):
    def monotonic(self) -> float: ...


class AccountLeaseRegistry:
    """In-process fenced leases; the production service currently has one worker."""

    def __init__(self, *, clock: _MonotonicClock | None = None, ttl_seconds: float = 30.0) -> None:
        if not 1.0 <= float(ttl_seconds) <= 300.0:
            raise ValueError("ttl_seconds must be between 1 and 300")
        self._clock = clock or time
        self._ttl_seconds = float(ttl_seconds)
        self._leases: dict[str, AccountLease] = {}
        self._lock = Lock()

    def try_acquire(self, account_id: str) -> AccountLease | None:
        account = str(account_id).strip()
        if not account:
            raise ValueError("account_id is required")
        now = float(self._clock.monotonic())
        with self._lock:
            existing = self._leases.get(account)
            if existing is not None and existing.expires_at > now:
                return None
            lease = AccountLease(account, uuid4().hex, now + self._ttl_seconds)
            self._leases[account] = lease
            return lease

    def release(self, lease: AccountLease) -> bool:
        with self._lock:
            current = self._leases.get(lease.account_id)
            if current is None or current.lease_id != lease.lease_id:
                return False
            del self._leases[lease.account_id]
            return True


class _AccountSource(Protocol):
    async def list_accounts(self) -> list[dict[str, Any]]: ...


class _Clock(_MonotonicClock, Protocol):
    async def sleep(self, seconds: float) -> None: ...


class _SystemClock:
    @staticmethod
    def monotonic() -> float:
        return time.monotonic()

    @staticmethod
    async def sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)


def _account_id(account: Mapping[str, Any]) -> str:
    return str(account.get("account_id") or account.get("id") or account.get("phone") or "").strip()


def _eligible_account(account: Mapping[str, Any]) -> bool:
    online = account.get("online") is True or str(account.get("status") or "").lower() == "online"
    risk = str(account.get("risk_status") or "normal").strip().lower()
    risk_ok = risk in {"", "normal", "ok", "safe", "passed"}
    return bool(_account_id(account) and online and risk_ok and account.get("is_wplus") is True and account.get("token"))


def _order_id(payload: Mapping[str, Any]) -> str:
    nested = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
    value = payload.get("order_id") or payload.get("orderId") or nested.get("order_id") or nested.get("orderId")
    return str(value or "").strip()


def _seat_ids_released(payload: Mapping[str, Any], expected: set[str]) -> bool:
    available = payload.get("available_seat_ids")
    if not isinstance(available, Sequence) or isinstance(available, (str, bytes)):
        return False
    return expected.issubset({str(value) for value in available})


class WandaDirectGateway:
    def __init__(
        self,
        *,
        account_source: _AccountSource,
        client_factory: Any,
        lease_registry: AccountLeaseRegistry,
        clock: _Clock | None = None,
        logger: Any = None,
        release_recheck_delays: Sequence[float] = (0.0, 2.0, 5.0),
    ) -> None:
        delays = tuple(float(value) for value in release_recheck_delays)
        if not delays or delays[0] != 0.0 or any(value < 0 for value in delays):
            raise ValueError("release_recheck_delays must begin with zero and stay non-negative")
        self._account_source = account_source
        self._client_factory = client_factory
        self._leases = lease_registry
        self._clock = clock or _SystemClock()
        self._logger = logger
        self._release_recheck_delays = delays

    async def probe_activity_offers(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        normalized = self._validate_request(request)
        accounts = [item for item in await self._account_source.list_accounts() if isinstance(item, Mapping) and _eligible_account(item)]
        if not accounts:
            raise DirectGatewayError("wplus_account_unavailable")

        lease_seen = False
        retryable_failure_seen = False
        for raw_account in accounts:
            account = dict(raw_account)
            account_id = _account_id(account)
            lease = self._leases.try_acquire(account_id)
            if lease is None:
                lease_seen = True
                continue
            client = self._client_factory(account)
            order_id = ""
            try:
                try:
                    created = await client.create_order(normalized)
                    if not isinstance(created, Mapping):
                        raise TypeError("invalid create response")
                    order_id = _order_id(created)
                    if not order_id:
                        raise DirectGatewayError("temporary_lock_state_unknown")
                except DirectGatewayError as error:
                    if error.retryable_before_create:
                        retryable_failure_seen = True
                        continue
                    raise
                except Exception as error:
                    raise DirectGatewayError("temporary_lock_state_unknown") from error

                offer_error: DirectGatewayError | None = None
                offers: Mapping[str, Any] | None = None
                try:
                    candidate = await client.activity_offers(
                        order_id=order_id,
                        cinema_id=normalized["cinema_id"],
                        showtime_id=normalized["showtime_id"],
                        partition=normalized["partition"],
                    )
                    if not isinstance(candidate, Mapping):
                        raise TypeError("invalid offer response")
                    offers = candidate
                except Exception as error:
                    offer_error = DirectGatewayError("activity_offers_failed")
                    offer_error.__cause__ = error

                cancelled, released = await self._cancel_and_verify(
                    client, order_id, normalized["showtime_id"], set(normalized["seat_ids"])
                )
                if not cancelled or not released:
                    raise DirectGatewayError("temporary_lock_release_unverified")
                if offer_error is not None:
                    raise offer_error
                return {
                    "account_id": account_id,
                    "offers": offers,
                    "release_verified": True,
                }
            finally:
                self._leases.release(lease)

        if lease_seen and not retryable_failure_seen:
            raise DirectGatewayError("account_lease_unavailable")
        raise DirectGatewayError("temporary_lock_failed")

    async def _cancel_and_verify(
        self, client: Any, order_id: str, showtime_id: str, expected_seat_ids: set[str]
    ) -> tuple[bool, bool]:
        try:
            cancelled = await client.cancel_order(order_id)
        except Exception:
            cancelled = False
        released = False
        for index, delay in enumerate(self._release_recheck_delays):
            if index > 0 and delay > 0:
                await self._clock.sleep(delay)
            try:
                snapshot = await client.realtime_seats(showtime_id)
                released = isinstance(snapshot, Mapping) and _seat_ids_released(snapshot, expected_seat_ids)
            except Exception:
                released = False
            if released:
                break
        return bool(cancelled), released

    @staticmethod
    def _validate_request(request: Mapping[str, Any]) -> dict[str, Any]:
        required_text = ("cinema_id", "showtime_id", "partition")
        normalized = {key: str(request.get(key) or "").strip() for key in required_text}
        seat_ids = request.get("seat_ids")
        if any(not value for value in normalized.values()) or not isinstance(seat_ids, Sequence) or isinstance(seat_ids, (str, bytes)):
            raise DirectGatewayError("invalid_direct_lock_request")
        normalized["seat_ids"] = [str(value).strip() for value in seat_ids if str(value).strip()]
        seat_payloads = request.get("seat_payloads")
        if isinstance(seat_payloads, Sequence) and not isinstance(seat_payloads, (str, bytes)):
            normalized["seat_payloads"] = [str(value).strip() for value in seat_payloads if str(value).strip()]
        total = request.get("total_price_cents")
        if not normalized["seat_ids"] or not isinstance(total, int) or isinstance(total, bool) or total <= 0:
            raise DirectGatewayError("invalid_direct_lock_request")
        normalized["total_price_cents"] = total
        return normalized
