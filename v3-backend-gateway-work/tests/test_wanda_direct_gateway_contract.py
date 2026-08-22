"""TDD contracts for the planned direct Wanda temporary-lock gateway.

The production module intentionally does not exist yet.  These tests define the
small dependency-injected API that its implementation must satisfy without
using the legacy ticket gateway or making a real Wanda order.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import socket
from collections.abc import Mapping
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest


MODULE_NAME = "app.wanda_direct_gateway"
REQUIRED_EXPORTS = (
    "AccountLeaseRegistry",
    "DirectGatewayError",
    "WandaDirectGateway",
)
FORBIDDEN_URL_PARTS = (
    "/api/order",
    "ticket-gateway",
    "127.0.0.1:8000",
    "localhost:8000",
)
REQUEST = {
    "cinema_id": "cinema-contract",
    "showtime_id": "showtime-contract",
    "seat_ids": ["seat-contract"],
    "partition": "area-contract-seat-contract",
    "total_price_cents": 5000,
}
OFFERS = {
    "activities": [
        {
            "name": "W+会员专享优惠",
            "able": True,
            "allot_seat": {"totalPayPrice": 4500},
        }
    ]
}


def _contract() -> SimpleNamespace:
    """Load the planned public API and report all absent entry points together."""
    try:
        module = importlib.import_module(MODULE_NAME)
    except ModuleNotFoundError as error:
        if error.name != MODULE_NAME:
            raise
        pytest.fail(
            "缺失计划接口 app.wanda_direct_gateway；应提供 "
            "AccountLeaseRegistry、DirectGatewayError、WandaDirectGateway",
            pytrace=False,
        )
    missing = [name for name in REQUIRED_EXPORTS if not hasattr(module, name)]
    assert not missing, f"{MODULE_NAME} 缺失接口: {', '.join(missing)}"
    return SimpleNamespace(module=module, **{name: getattr(module, name) for name in REQUIRED_EXPORTS})


class FakeClock:
    def __init__(self) -> None:
        self.value = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def now(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds

    async def sleep(self, seconds: float) -> None:
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.advance(seconds)
        await asyncio.sleep(0)


class FakeLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(self, level: str, *args: Any, **kwargs: Any) -> None:
        self.records.append((level, args, kwargs))

    def debug(self, *args: Any, **kwargs: Any) -> None:
        self._record("debug", *args, **kwargs)

    def info(self, *args: Any, **kwargs: Any) -> None:
        self._record("info", *args, **kwargs)

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self._record("warning", *args, **kwargs)

    def error(self, *args: Any, **kwargs: Any) -> None:
        self._record("error", *args, **kwargs)

    def rendered(self) -> str:
        return repr(self.records)


class FakeAccountSource:
    def __init__(self, accounts: list[dict[str, Any]]) -> None:
        self.accounts = accounts
        self.reads = 0

    async def list_accounts(self) -> list[dict[str, Any]]:
        self.reads += 1
        return deepcopy(self.accounts)


class FakeOfficialClient:
    """High-level fake for the official app API; it never performs HTTP."""

    def __init__(
        self,
        account_id: str,
        *,
        create_error: BaseException | None = None,
        offers_error: BaseException | None = None,
        cancel_succeeds: bool = True,
        release_snapshots: list[bool] | None = None,
        block_create: bool = False,
        offers: Mapping[str, Any] | None = None,
        create_verified: bool = True,
    ) -> None:
        self.account_id = account_id
        self.create_error = create_error
        self.offers_error = offers_error
        self.cancel_succeeds = cancel_succeeds
        self.release_snapshots = list(release_snapshots or [True])
        self.offers = deepcopy(OFFERS if offers is None else offers)
        self.create_verified = create_verified
        self.events: list[tuple[str, str]] = []
        self.raw_urls: list[str] = []
        self.create_started = asyncio.Event()
        self.allow_create = asyncio.Event()
        if not block_create:
            self.allow_create.set()

    async def create_order(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.events.append((self.account_id, "create_order"))
        assert dict(request) == REQUEST
        self.create_started.set()
        await self.allow_create.wait()
        if self.create_error is not None:
            raise self.create_error
        return {"order_id": f"temporary-{self.account_id}", "create_verified": self.create_verified}

    async def activity_offers(
        self,
        *,
        order_id: str,
        cinema_id: str,
        showtime_id: str,
        partition: str,
    ) -> Mapping[str, Any]:
        self.events.append((self.account_id, "activity_offers"))
        assert order_id == f"temporary-{self.account_id}"
        assert (cinema_id, showtime_id, partition) == (
            REQUEST["cinema_id"],
            REQUEST["showtime_id"],
            REQUEST["partition"],
        )
        if self.offers_error is not None:
            raise self.offers_error
        return deepcopy(self.offers)

    async def cancel_order(self, order_id: str) -> bool:
        self.events.append((self.account_id, "cancel_order"))
        assert order_id == f"temporary-{self.account_id}"
        return self.cancel_succeeds

    async def realtime_seats(self, showtime_id: str) -> Mapping[str, Any]:
        self.events.append((self.account_id, "realtime_seats"))
        assert showtime_id == REQUEST["showtime_id"]
        released = self.release_snapshots.pop(0) if self.release_snapshots else False
        return {"available_seat_ids": list(REQUEST["seat_ids"]) if released else []}

    async def request(self, method: str, url: str, **_kwargs: Any) -> Mapping[str, Any]:
        """Trap accidental fallback to a URL-based ticket gateway."""
        self.raw_urls.append(url)
        raise AssertionError(f"direct gateway attempted raw request: {method} {url}")


class FakeClientFactory:
    def __init__(self, clients: Mapping[str, FakeOfficialClient]) -> None:
        self.clients = dict(clients)
        self.created_for: list[str] = []

    def __call__(self, account: Mapping[str, Any]) -> FakeOfficialClient:
        account_id = str(account["account_id"])
        self.created_for.append(account_id)
        return self.clients[account_id]


def account(
    account_id: str,
    *,
    online: bool = True,
    risk_status: str = "normal",
    is_wplus: bool = True,
    token: str | None = None,
    remaining: object = None,
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "display_name": f"masked-{account_id}",
        "online": online,
        "risk_status": risk_status,
        "is_wplus": is_wplus,
        "token": token if token is not None else f"contract-secret-{account_id}",
        "remaining": remaining,
    }


def build_gateway(
    contract: SimpleNamespace,
    accounts: list[dict[str, Any]],
    clients: Mapping[str, FakeOfficialClient],
    *,
    clock: FakeClock | None = None,
    logger: FakeLogger | None = None,
    lease_ttl_seconds: float = 30.0,
    delayed_release_recheck_delays: tuple[float, ...] = (15.0, 15.0),
    pricing_ref_key: str = "contract-pricing-reference-key-0001",
) -> tuple[Any, FakeClock, FakeLogger, FakeClientFactory]:
    fake_clock = clock or FakeClock()
    fake_logger = logger or FakeLogger()
    factory = FakeClientFactory(clients)
    leases = contract.AccountLeaseRegistry(clock=fake_clock, ttl_seconds=lease_ttl_seconds)
    gateway = contract.WandaDirectGateway(
        account_source=FakeAccountSource(accounts),
        client_factory=factory,
        lease_registry=leases,
        pricing_ref_key=pricing_ref_key,
        clock=fake_clock,
        logger=fake_logger,
        release_recheck_delays=(0.0, 2.0, 5.0),
        delayed_release_recheck_delays=delayed_release_recheck_delays,
    )
    return gateway, fake_clock, fake_logger, factory


async def probe(gateway: Any) -> Mapping[str, Any]:
    return await gateway.probe_activity_offers(REQUEST)


def assert_error_code(error: BaseException, expected: str) -> None:
    assert getattr(error, "code", None) == expected
    assert expected in str(error)


def test_planned_direct_gateway_exports_the_contract_entry_points() -> None:
    _contract()


def test_selects_only_online_normal_risk_wplus_account_with_token_and_redacts_token() -> None:
    contract = _contract()
    secret = "contract-secret-eligible"
    accounts = [
        account("offline", online=False),
        account("risk-blocked", risk_status="blocked"),
        account("not-wplus", is_wplus=False),
        account("missing-token", token=""),
        account("exhausted", remaining=0),
        account("malformed-quota", remaining="1"),
        account("eligible", token=secret),
    ]
    client = FakeOfficialClient("eligible")
    gateway, _clock, logger, factory = build_gateway(contract, accounts, {"eligible": client})

    result = asyncio.run(probe(gateway))

    assert factory.created_for == ["eligible"]
    assert len(result["pricing_account_ref"]) == 32
    assert result["pricing_account_ref"] != "eligible"
    assert "account_id" not in result
    assert result["offers"] == OFFERS
    assert result["release_verified"] is True
    serialized = json.dumps(result, ensure_ascii=False, default=repr)
    assert secret not in serialized
    assert "token" not in result
    assert secret not in logger.rendered()


def test_pricing_account_reference_is_stable_across_token_rotation_and_keyed_server_side() -> None:
    contract = _contract()
    reference_key = "contract-pricing-reference-key-0001"
    first = FakeOfficialClient("eligible")
    first_gateway, _clock, _logger, _factory = build_gateway(
        contract, [account("eligible", token="first-rotating-token")], {"eligible": first},
        pricing_ref_key=reference_key,
    )
    second = FakeOfficialClient("eligible")
    second_gateway, _clock, _logger, _factory = build_gateway(
        contract, [account("eligible", token="second-rotating-token")], {"eligible": second},
        pricing_ref_key=reference_key,
    )

    first_ref = asyncio.run(probe(first_gateway))["pricing_account_ref"]
    second_ref = asyncio.run(probe(second_gateway))["pricing_account_ref"]
    assert first_ref == second_ref
    assert "eligible" not in first_ref
    with pytest.raises(ValueError, match="pricing_ref_key"):
        build_gateway(contract, [account("eligible")], {"eligible": first}, pricing_ref_key="too-short")


def test_account_lease_is_bounded_fenced_and_cannot_be_released_by_an_expired_holder() -> None:
    contract = _contract()
    clock = FakeClock()
    leases = contract.AccountLeaseRegistry(clock=clock, ttl_seconds=10.0)

    first = leases.try_acquire("eligible")
    assert first is not None
    assert 0 < first.expires_at - clock.monotonic() <= 10.0
    assert leases.try_acquire("eligible") is None
    clock.advance(9.0)
    renewed = leases.renew(first, min_ttl_seconds=40.0)
    assert renewed is not None
    assert renewed.expires_at - clock.monotonic() >= 40.0
    assert leases.try_acquire("eligible") is None

    clock.advance(40.0)
    second = leases.try_acquire("eligible")
    assert second is not None
    assert second.lease_id != first.lease_id
    assert leases.renew(first, min_ttl_seconds=40.0) is None
    assert leases.release(first) is False
    assert leases.try_acquire("eligible") is None
    assert leases.release(second) is True
    assert leases.try_acquire("eligible") is not None


def test_delayed_release_recheck_window_is_strictly_bounded() -> None:
    contract = _contract()
    client = FakeOfficialClient("eligible")
    with pytest.raises(ValueError, match="delayed_release_recheck_delays"):
        build_gateway(
            contract, [account("eligible")], {"eligible": client},
            delayed_release_recheck_delays=(0.0, 61.0),
        )


def test_same_account_cannot_create_two_temporary_orders_concurrently() -> None:
    contract = _contract()

    async def scenario() -> None:
        client = FakeOfficialClient("only", block_create=True)
        gateway, _clock, _logger, factory = build_gateway(contract, [account("only")], {"only": client})
        first = asyncio.create_task(probe(gateway))
        await client.create_started.wait()

        with pytest.raises(contract.DirectGatewayError) as raised:
            await probe(gateway)
        assert_error_code(raised.value, "account_lease_unavailable")
        assert [event for event in client.events if event[1] == "create_order"] == [("only", "create_order")]

        client.allow_create.set()
        result = await first
        assert result["release_verified"] is True
        assert factory.created_for == ["only"]

    asyncio.run(scenario())


def test_direct_official_client_keeps_one_account_for_create_offers_cancel_and_0_2_5_rechecks() -> None:
    contract = _contract()
    client = FakeOfficialClient("eligible", release_snapshots=[False, False, True])
    gateway, clock, _logger, factory = build_gateway(contract, [account("eligible")], {"eligible": client})

    result = asyncio.run(probe(gateway))

    assert result["release_verified"] is True
    assert clock.sleeps == [2.0, 5.0]
    assert client.events == [
        ("eligible", "create_order"),
        ("eligible", "activity_offers"),
        ("eligible", "cancel_order"),
        ("eligible", "realtime_seats"),
        ("eligible", "realtime_seats"),
        ("eligible", "realtime_seats"),
    ]
    assert factory.created_for == ["eligible"]


def test_unverified_create_with_order_id_is_cancelled_and_not_treated_as_locked() -> None:
    contract = _contract()
    client = FakeOfficialClient("uncertain", create_verified=False)
    gateway, _clock, _logger, factory = build_gateway(
        contract, [account("uncertain")], {"uncertain": client}
    )

    try:
        asyncio.run(probe(gateway))
    except contract.DirectGatewayError as error:
        assert_error_code(error, "temporary_lock_state_unknown")
    else:
        raise AssertionError("bizCode failure with an order id was accepted")

    assert factory.created_for == ["uncertain"]
    assert [event[1] for event in client.events] == ["create_order", "cancel_order", "realtime_seats"]


def test_missing_wplus_offer_retries_next_account_only_after_release() -> None:
    contract = _contract()
    first = FakeOfficialClient("first", offers={"activities": []})
    second = FakeOfficialClient("second")
    gateway, _clock, _logger, factory = build_gateway(
        contract,
        [account("first"), account("second")],
        {"first": first, "second": second},
    )

    result = asyncio.run(probe(gateway))

    assert len(result["pricing_account_ref"]) == 32
    assert "account_id" not in result
    assert factory.created_for == ["first", "second"]
    assert [event[1] for event in first.events] == ["create_order", "activity_offers", "cancel_order", "realtime_seats"]
    assert [event[1] for event in second.events] == ["create_order", "activity_offers", "cancel_order", "realtime_seats"]


def test_sequential_probes_rotate_across_the_available_account_pool() -> None:
    contract = _contract()
    first = FakeOfficialClient("first", release_snapshots=[True, True])
    second = FakeOfficialClient("second", release_snapshots=[True, True])
    gateway, _clock, _logger, factory = build_gateway(
        contract,
        [account("first"), account("second")],
        {"first": first, "second": second},
    )

    first_ref = asyncio.run(probe(gateway))["pricing_account_ref"]
    second_ref = asyncio.run(probe(gateway))["pricing_account_ref"]
    assert len(first_ref) == 32 and len(second_ref) == 32
    assert first_ref != second_ref
    assert factory.created_for == ["first", "second"]


def test_create_failure_may_try_next_account_before_any_order_exists() -> None:
    contract = _contract()
    first_secret = "contract-secret-first"
    second_secret = "contract-secret-second"
    first = FakeOfficialClient("first")
    first.create_error = contract.DirectGatewayError(
        "pre_create_account_unavailable", retryable_before_create=True
    )
    second = FakeOfficialClient("second")
    gateway, _clock, logger, factory = build_gateway(
        contract,
        [account("first", token=first_secret), account("second", token=second_secret)],
        {"first": first, "second": second},
    )

    result = asyncio.run(probe(gateway))

    assert len(result["pricing_account_ref"]) == 32
    assert "account_id" not in result
    serialized = json.dumps(result, ensure_ascii=False, default=repr)
    assert first_secret not in serialized and second_secret not in serialized
    assert first_secret not in logger.rendered() and second_secret not in logger.rendered()
    assert first.events == [("first", "create_order")]
    assert second.events == [
        ("second", "create_order"),
        ("second", "activity_offers"),
        ("second", "cancel_order"),
        ("second", "realtime_seats"),
    ]
    assert factory.created_for == ["first", "second"]


def test_unknown_create_result_never_switches_account_without_proof_no_order_exists() -> None:
    contract = _contract()
    first = FakeOfficialClient("first", create_error=RuntimeError("synthetic unknown create result"))
    second = FakeOfficialClient("second")
    gateway, _clock, _logger, factory = build_gateway(
        contract,
        [account("first"), account("second")],
        {"first": first, "second": second},
    )

    with pytest.raises(contract.DirectGatewayError) as raised:
        asyncio.run(probe(gateway))

    assert_error_code(raised.value, "temporary_lock_state_unknown")
    assert factory.created_for == ["first"]
    assert first.events == [("first", "create_order")]
    assert second.events == []


def test_failure_after_create_never_switches_account_and_finally_cancels_same_order() -> None:
    contract = _contract()
    first = FakeOfficialClient("first", offers_error=RuntimeError("synthetic offers failure"))
    second = FakeOfficialClient("second")
    gateway, _clock, _logger, factory = build_gateway(
        contract,
        [account("first"), account("second")],
        {"first": first, "second": second},
    )

    with pytest.raises(contract.DirectGatewayError) as raised:
        asyncio.run(probe(gateway))

    assert_error_code(raised.value, "activity_offers_failed")
    assert factory.created_for == ["first"]
    assert first.events == [
        ("first", "create_order"),
        ("first", "activity_offers"),
        ("first", "cancel_order"),
        ("first", "realtime_seats"),
    ]
    assert second.events == []


def test_cancel_success_without_seat_restoration_is_release_unverified() -> None:
    contract = _contract()
    client = FakeOfficialClient("eligible", cancel_succeeds=True, release_snapshots=[False, False, False])
    gateway, clock, _logger, _factory = build_gateway(contract, [account("eligible")], {"eligible": client})

    with pytest.raises(contract.DirectGatewayError) as raised:
        asyncio.run(probe(gateway))

    assert_error_code(raised.value, "temporary_lock_release_unverified")
    assert clock.sleeps[:2] == [2.0, 5.0]
    assert [event[1] for event in client.events].count("realtime_seats") >= 3


def test_release_failure_stays_failed_while_background_recheck_confirms_late_restoration() -> None:
    contract = _contract()

    async def scenario() -> None:
        secret = "contract-secret-eligible"
        client = FakeOfficialClient(
            "eligible", cancel_succeeds=True,
            release_snapshots=[False, False, False, True, True],
        )
        gateway, clock, logger, _factory = build_gateway(
            contract, [account("eligible", token=secret)], {"eligible": client},
            lease_ttl_seconds=180.0,
        )

        with pytest.raises(contract.DirectGatewayError) as raised:
            await probe(gateway)
        assert_error_code(raised.value, "temporary_lock_release_unverified")

        with pytest.raises(contract.DirectGatewayError) as leased:
            await probe(gateway)
        assert_error_code(leased.value, "account_lease_unavailable")

        await gateway.wait_for_background_rechecks()
        assert clock.sleeps == [2.0, 5.0, 15.0]
        assert [event[1] for event in client.events].count("realtime_seats") == 4
        assert "release_confirmed_after_failure" in logger.rendered()
        assert secret not in logger.rendered()
        assert "temporary-eligible" not in logger.rendered()

        result = await probe(gateway)
        assert result["release_verified"] is True

    asyncio.run(scenario())


def test_background_release_recheck_is_bounded_and_never_changes_failed_quote_result() -> None:
    contract = _contract()

    async def scenario() -> None:
        client = FakeOfficialClient(
            "eligible", cancel_succeeds=True,
            release_snapshots=[False, False, False, False, False],
        )
        gateway, clock, logger, _factory = build_gateway(
            contract, [account("eligible")], {"eligible": client},
            lease_ttl_seconds=10.0,
        )

        with pytest.raises(contract.DirectGatewayError) as raised:
            await probe(gateway)
        assert_error_code(raised.value, "temporary_lock_release_unverified")
        await gateway.wait_for_background_rechecks()

        assert clock.sleeps == [2.0, 5.0, 15.0, 15.0]
        assert [event[1] for event in client.events].count("realtime_seats") == 5
        assert "release_still_unverified_after_background_recheck" in logger.rendered()

    asyncio.run(scenario())


def test_direct_flow_never_uses_legacy_order_or_ticket_system_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    contract = _contract()
    client = FakeOfficialClient("eligible")
    gateway, _clock, _logger, _factory = build_gateway(contract, [account("eligible")], {"eligible": client})
    attempted_connections: list[Any] = []

    def forbid_socket_connect(_socket: socket.socket, address: Any) -> None:
        attempted_connections.append(address)
        raise AssertionError(f"network access is forbidden in contract tests: {address!r}")

    async def scenario() -> Mapping[str, Any]:
        # Patch after the Windows proactor loop has created its internal
        # socketpair; only application network attempts are forbidden here.
        monkeypatch.setattr(socket.socket, "connect", forbid_socket_connect)
        return await probe(gateway)

    result = asyncio.run(scenario())

    assert result["release_verified"] is True
    assert attempted_connections == []
    assert client.raw_urls == []
    rendered_events = repr(client.events).lower()
    assert all(fragment not in rendered_events for fragment in FORBIDDEN_URL_PARTS)
