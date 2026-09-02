from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.probe.account_pool import FixtureProbeAccountPool, ProbeAccount
from app.probe.audit import ProbeAuditStore
from app.probe.coordinator import ProbeCoordinator, ProbeRequest
from app.probe.errors import ProbeError
from app.probe.lease_store import DurableAccountLeaseStore
from app.probe.models import ProbeOrder, ProbeResult, ProbeStatus
from app.probe.policy import ProbePolicy
from app.probe.probe_store import DurableProbeStore
from app.probe.release_tracker import FakeClock, ReleaseTracker
from app.probe.seat_selector import LiveSeat, ProbeSeatSelector
from app.probe.wanda_active_probe import FixtureWandaProvider, WandaActiveProbe
from app.agent.tools import build_read_only_registry
from app.errors import ProviderError


IDENTITY = {
    "tenant_id": "tenant-test",
    "shop_id": "shop-test",
    "show_id": "show-test",
}


def accounts(provider: FixtureWandaProvider | None = None) -> FixtureProbeAccountPool:
    return FixtureProbeAccountPool([
        ProbeAccount(
            account_ref="acct-test-1", online=True,
            is_wplus=True if provider is None else provider.is_wplus,
            token_present=True if provider is None else provider.token_present,
            phone_present=True, risk_status="normal",
            remaining=3 if provider is None else provider.remaining,
        ),
        ProbeAccount(
            account_ref="acct-offline", online=False, is_wplus=True,
            token_present=True, phone_present=True, risk_status="normal",
            remaining=3,
        ),
    ])


def seats() -> list[LiveSeat]:
    return [
        LiveSeat(seat_id="s-1", label="12排16座", area_code="A", zone_type="W+", available=True, wplus=True),
        LiveSeat(seat_id="s-2", label="12排17座", area_code="A", zone_type="W+", available=True, wplus=True),
        LiveSeat(seat_id="s-3", label="13排1座", area_code="B", zone_type="普通", available=True, wplus=False),
    ]


def request(**updates: object) -> ProbeRequest:
    return ProbeRequest(**{**IDENTITY, "requested_seat_labels": ["12排16座", "12排17座"], **updates})


def build(tmp_path: Path, provider: FixtureWandaProvider, *, enabled: bool = True):
    db = tmp_path / "probe.sqlite3"
    probe_store = DurableProbeStore(db)
    lease_store = DurableAccountLeaseStore(db)
    tracker = ReleaseTracker(probe_store, clock=FakeClock())
    active = WandaActiveProbe(
        provider, probe_store=probe_store, release_tracker=tracker,
        policy=ProbePolicy(active_probe_enabled=enabled, agent_harness_read_only=False),
    )
    coordinator = ProbeCoordinator(
        probe_store=probe_store, lease_store=lease_store, account_pool=accounts(provider),
        seat_selector=ProbeSeatSelector(), active_probe=active,
        policy=ProbePolicy(active_probe_enabled=enabled, agent_harness_read_only=False),
        lease_ttl_seconds=180,
    )
    return coordinator, probe_store, lease_store


@pytest.mark.asyncio
async def test_success_returns_probe_facts_only_and_releases(tmp_path: Path) -> None:
    provider = FixtureWandaProvider(member_price_cents=3800)
    coordinator, store, leases = build(tmp_path, provider)

    result = await coordinator.run(request(), seats())

    assert result.status == "SUCCESS"
    assert result.seat_type_prices[0].member_price_cents == 3800
    assert result.release_verified is True
    assert result.model_dump().keys() == {
        "probe_id", "provider", "show_id", "status", "seat_type_prices",
        "release_verified", "error_code",
    }
    assert provider.calls == ["create_order", "order_status", "activity", "read_member_prices", "cancel_order", "order_status", "available_seat_ids"]
    order = store.get(result.probe_id)
    assert order is not None and order.status == ProbeStatus.RELEASE_VERIFIED
    assert leases.get("acct-test-1") is None


@pytest.mark.asyncio
async def test_kill_switch_fails_closed_without_provider_call(tmp_path: Path) -> None:
    provider = FixtureWandaProvider()
    coordinator, _, _ = build(tmp_path, provider, enabled=False)

    with pytest.raises(ProbeError, match="active_probe_disabled"):
        await coordinator.run(request(), seats())
    assert provider.calls == []


@pytest.mark.asyncio
async def test_read_only_also_blocks_probe() -> None:
    policy = ProbePolicy(active_probe_enabled=True, agent_harness_read_only=True)
    with pytest.raises(ProbeError, match="agent_harness_read_only"):
        policy.ensure_allowed()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", json.loads(
    (Path(__file__).parent / "fixtures" / "probe_golden.json").read_text(encoding="utf-8")
))
async def test_probe_golden_scenarios_are_probe_only(tmp_path: Path, scenario: dict[str, object]) -> None:
    provider = FixtureWandaProvider.from_scenario(scenario)
    coordinator, store, _ = build(tmp_path, provider)
    if scenario["name"] == "active_probe_disabled":
        pytest.skip("kill switch is covered by the dedicated test")
    result = await coordinator.run(request(), seats())
    assert result.error_code == scenario["expected_error_code"]
    assert result.release_verified is scenario["expected_release_verified"]
    assert result.status in {"SUCCESS", "FAILED"}
    assert "selling_price" not in result.model_dump()
    assert "quote_price" not in result.model_dump()
    assert "markup" not in result.model_dump()
    order = store.get(result.probe_id)
    assert order is not None
    assert order.status.value in set(scenario["expected_terminal_statuses"])


@pytest.mark.asyncio
async def test_cleanup_survives_agent_turn_cancellation(tmp_path: Path) -> None:
    provider = FixtureWandaProvider(member_price_cents=3800, cancel_delay_seconds=0.01)
    coordinator, store, _ = build(tmp_path, provider)
    task = asyncio.create_task(coordinator.run(request(), seats()))
    await provider.created.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.03)
    order = next(item for item in store.list_all() if item.temporary_order_reference)
    assert "cancel_order" in provider.calls
    assert order.status in {ProbeStatus.RELEASE_VERIFIED, ProbeStatus.RELEASE_UNVERIFIED}


def test_exact_selection_groups_without_cross_area_averaging() -> None:
    selected = ProbeSeatSelector().select_exact(["12排16座", "12排17座"], seats())
    assert [item.seat_id for item in selected] == ["s-1"]
    assert [(item.area_code, item.zone_type) for item in selected] == [("A", "W+")]


def test_area_selection_uses_only_available_wplus_representatives() -> None:
    selected = ProbeSeatSelector().select_area(seats())
    assert [item.seat_id for item in selected] == ["s-1"]


def test_durable_lease_expiry_recovery(tmp_path: Path) -> None:
    store = DurableAccountLeaseStore(tmp_path / "leases.sqlite3")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert store.try_acquire("a", "p1", "show", now=now, ttl_seconds=180)
    assert not store.try_acquire("a", "p2", "show", now=now, ttl_seconds=180)
    assert store.recover_expired(now=now + timedelta(seconds=181)) == ["a"]
    assert store.try_acquire("a", "p2", "show", now=now + timedelta(seconds=181), ttl_seconds=180)


def test_durable_probe_revision_and_restart_recovery(tmp_path: Path) -> None:
    path = tmp_path / "probe.sqlite3"
    first = DurableProbeStore(path)
    order = ProbeOrder(probe_id="probe-restart", tenant_id="t", shop_id="s", show_id="show", status=ProbeStatus.LOCKED)
    first.create(order)
    first.update("probe-restart", expected_revision=0, status=ProbeStatus.CANCEL_REQUESTED)
    second = DurableProbeStore(path)
    recovered = second.get("probe-restart")
    assert recovered is not None and recovered.status == ProbeStatus.CANCEL_REQUESTED
    with pytest.raises(ValueError, match="probe_revision_conflict"):
        second.update("probe-restart", expected_revision=0, status=ProbeStatus.RELEASE_CHECKING)


def test_release_tracker_uses_fake_clock_without_real_sleep(tmp_path: Path) -> None:
    store = DurableProbeStore(tmp_path / "probe.sqlite3")
    clock = FakeClock()
    tracker = ReleaseTracker(store, clock=clock)
    order = ProbeOrder(probe_id="probe-release", tenant_id="t", shop_id="s", show_id="show", status=ProbeStatus.CANCEL_CONFIRMED, seat_ids=["s-1"])
    store.create(order)
    provider = FixtureWandaProvider(available_after_seconds=5)
    verified = asyncio.run(tracker.verify(order, provider))
    assert verified is True
    assert clock.elapsed_seconds == 5


@pytest.mark.asyncio
async def test_quote_preview_disables_legacy_active_probe_context() -> None:
    class LegacyQuoteService:
        async def quote(self, _recognition: object) -> object:
            assert ProbePolicy.context_allows_active_probe() is False
            raise ProviderError("QUOTE_REQUIRES_ACTIVE_PROBE", "member cost missing")

    registry = build_read_only_registry(quote_service=LegacyQuoteService())
    observation = await registry.execute(
        "quote.preview",
        {"cinema_name": "测试影院", "movie_name": "测试影片", "date": "2026-09-02", "showtime_start": "12:00"},
        identity={"tenant_id": "t", "shop_id": "s", "buyer_id": "b", "chat_id": "c"},
    )
    assert observation.code == "QUOTE_REQUIRES_ACTIVE_PROBE"


def test_probe_result_rejects_commercial_pricing_fields() -> None:
    with pytest.raises(ValueError):
        ProbeResult(
            probe_id="p", show_id="show", status="SUCCESS", release_verified=True,
            selling_price=3900,
        )


def test_audit_store_redacts_sensitive_fields(tmp_path: Path) -> None:
    audit = ProbeAuditStore(tmp_path / "audit.sqlite3")
    audit.record("p", "provider_call", {"account_ref": "a", "token": "secret", "phone": "13800000000"})
    payload = audit.list_for("p")[0]["payload"]
    assert payload == {"account_ref": "a"}


def test_sensitive_account_fields_are_not_serialized_to_public_result() -> None:
    account = ProbeAccount(
        account_ref="safe-ref", online=True, is_wplus=True, token_present=True,
        phone_present=True, risk_status="normal", remaining=1,
        token="secret-token", phone="13800000000",
    )
    assert "secret-token" not in json.dumps(account.public_view())
    assert "13800000000" not in json.dumps(account.public_view())
    assert "token" not in json.dumps(account.public_view())
