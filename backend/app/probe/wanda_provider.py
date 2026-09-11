from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Callable

from app.errors import ProviderError
from app.wanda_direct_quote import APP_CHANNEL, FRONT_ORIGIN, MARKETING_ORIGIN

from .account_pool import ProbeAccount
from .canonical import ActivityOffersResult, CancelResult, CreateOrderResult, OrderStatusResult, SeatAvailabilityResult
from .errors import ProbeError
from .models import ProbeOrder
from .seat_selector import LiveSeat


class WandaProbeAccountPool:
    """Read the existing protected Wanda account pool for one W+ account.

    The pool exposes only a stable reference through ProbeOrder/audit records;
    tokens remain in this process and are never included in ``public_view``.
    """

    def __init__(self, wanda_service: Any, settings_provider: Callable[[], Any]) -> None:
        self._wanda = wanda_service
        self._settings_provider = settings_provider
        self._payloads: dict[str, dict[str, Any]] = {}

    def select(self, *, now: datetime | None = None) -> ProbeAccount:
        del now
        account = self._wanda._fixed_account(self._settings_provider())
        token = str(account.get("token") or "").strip()
        phone = str(account.get("phone") or "").strip()
        if not token or not phone:
            raise ProbeError("wplus_account_unavailable", "万达W+账号凭据不完整。")
        account_ref = hashlib.sha256(f"{phone}\0{token}".encode()).hexdigest()[:40]
        user_info = account.get("user_info") if isinstance(account.get("user_info"), Mapping) else {}
        try:
            remaining = int(account.get("probe_remaining", account.get("remaining", 1)))
        except (TypeError, ValueError):
            remaining = 0
        value = ProbeAccount(
            account_ref=account_ref,
            online=True,
            is_wplus=True,
            token_present=True,
            phone_present=True,
            risk_status=str(account.get("risk_status") or "normal"),
            remaining=max(0, remaining),
            token=token,
            phone=phone,
            user_identifier=str(user_info.get("userIdentifier") or "").strip() or None,
            shumei_box_id=str(account.get("shumei_box_id") or "").strip() or None,
        )
        self._payloads[account_ref] = dict(account)
        return value

    def payload(self, account_ref: str) -> dict[str, Any]:
        cached = self._payloads.get(str(account_ref).strip())
        if cached is not None:
            return dict(cached)
        account = self._wanda._fixed_account(self._settings_provider())
        token = str(account.get("token") or "").strip()
        phone = str(account.get("phone") or "").strip()
        actual = hashlib.sha256(f"{phone}\0{token}".encode()).hexdigest()[:40]
        if actual != str(account_ref).strip():
            raise ProbeError("probe_account_reference_mismatch", "Probe账号引用与当前账号不一致。")
        self._payloads[actual] = dict(account)
        return dict(account)


class WandaDirectProbeProvider:
    """Real Wanda provider for the already-frozen Probe lifecycle.

    All provider writes are routed through ``WandaDirectQuoteService``'s
    allow-listed, signed request helper. This adapter never creates customer
    orders; its create endpoint is used only by ``WandaActiveProbe`` and is
    always followed by cancellation and release verification.
    """

    fixture = False

    def __init__(self, wanda_service: Any, account_pool: WandaProbeAccountPool) -> None:
        self._wanda = wanda_service
        self._account_pool = account_pool
        self._live_seats: dict[str, LiveSeat] = {}
        self._current_account_ref: str | None = None
        self._current_show_id: str | None = None
        self._current_seat_id: str | None = None

    def bind_live_seats(self, seats: list[LiveSeat]) -> None:
        self._live_seats = {seat.seat_id: seat for seat in seats}

    def bind_probe_order(self, order: ProbeOrder) -> None:
        self._current_account_ref = order.account_ref or None
        self._current_show_id = order.show_id
        self._current_seat_id = order.seat_ids[0] if len(order.seat_ids) == 1 else None

    def _account(self, account: ProbeAccount | None = None, account_ref: str | None = None) -> dict[str, Any]:
        ref = account.account_ref if account is not None else account_ref
        if not ref:
            raise ProbeError("probe_account_reference_missing", "Probe账号引用缺失。")
        payload = self._account_pool.payload(ref)
        if account is not None:
            payload.setdefault("token", account.token)
            payload.setdefault("phone", account.phone)
        return payload

    async def create_probe_order(self, *, account: ProbeAccount, show_id: str, seat_ids: list[str]) -> CreateOrderResult:
        if len(seat_ids) != 1:
            raise ProbeError("probe_seat_count_unsupported", "实时Probe一次只允许锁定一个代表座位。")
        if not account.eligible(now=datetime.now(timezone.utc)):
            raise ProbeError("wplus_account_unavailable", "万达W+账号当前不可执行Probe。")
        self._current_account_ref = account.account_ref
        self._current_show_id = str(show_id)
        self._current_seat_id = str(seat_ids[0])
        seat = self._live_seats.get(str(seat_ids[0]))
        if seat is None or not seat.available or not seat.wplus or not seat.original_price_cents:
            raise ProbeError("probe_seat_not_bound", "Probe代表座位未通过实时座位校验。")
        try:
            payload = await self._wanda._official_app_request(
                self._account(account), FRONT_ORIGIN, "/order/create_order.api", method="POST",
            pairs=[
                ("retailerCode", "MX"), ("mobile", str(account.phone or "")),
                ("seatId", f"{seat.seat_id},{seat.original_price_cents},0,0"),
                ("totalPrice", seat.original_price_cents), ("dId", show_id),
                ], sign_encoded=True, event="wanda_probe_create_order_response",
            )
        except ProviderError as error:
            raise ProbeError(error.code, error.message) from error
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        reference = str(data.get("orderId") or payload.get("orderId") or "").strip()
        code = payload.get("code")
        biz_code = data.get("bizCode")
        outcome = "CONFIRMED" if reference and code in (0, "0") and biz_code in (None, 0, "0") else "FAILED"
        return CreateOrderResult(
            code=code, biz_code=biz_code, temporary_order_id=reference or None, outcome=outcome,
        )

    async def get_order_status(self, *, temporary_order_reference: str) -> OrderStatusResult:
        payload = await self._wanda._official_app_request(
            self._account(account_ref=self._find_account_ref()), FRONT_ORIGIN,
            "/order/order_status.api", method="POST",
            pairs=[("json", "true"), ("orderId", temporary_order_reference)],
            event="wanda_probe_order_status_response",
        )
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        try:
            lock_time = int(data.get("lockSeatTime"))
        except (TypeError, ValueError):
            lock_time = None
        return OrderStatusResult(order_status=data.get("orderStatus"), lock_seat_time=lock_time)

    async def get_activity_offers(self, *, temporary_order_reference: str) -> ActivityOffersResult:
        account = self._account(account_ref=self._find_account_ref())
        payload = await self._wanda._official_app_request(
            account, MARKETING_ORIGIN, "/mkt/activity/secret/list.api", method="GET",
            pairs=[("partition", self._partition_for_current_seat()),
                   ("orderId", temporary_order_reference), ("did", self._show_id_hint())],
            event="wanda_probe_activity_response",
        )
        price = self._wanda._wplus_offer_price(payload)
        return ActivityOffersResult(
            able=True, name="W+会员专享", total_pay_price_cents=price, member_price_cents=price,
        )

    async def cancel_probe_order(self, *, temporary_order_reference: str) -> CancelResult:
        payload = await self._wanda._official_app_request(
            self._account(account_ref=self._find_account_ref()), FRONT_ORIGIN,
            "/order/cancel.api", method="POST", pairs=[("orderId", temporary_order_reference)],
            event="wanda_probe_cancel_response",
        )
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        return CancelResult(
            accepted=payload.get("code") in (0, "0", None)
            and data.get("bizCode") in (0, "0", None),
        )

    async def get_available_seats(self, *, show_id: str, seat_ids: list[str]) -> SeatAvailabilityResult:
        payload = await self._wanda._official_get(
            self._account(account_ref=self._find_account_ref()), FRONT_ORIGIN,
            "/order/real_time_seat.api", [("dId", show_id)], channel=APP_CHANNEL,
            event="wanda_probe_release_seats_response",
        )
        available = {
            seat_id for seat_id in seat_ids
            if self._wanda._seat_id_available(payload, seat_id)
        }
        return SeatAvailabilityResult(available_seat_ids=available)

    def _find_account_ref(self) -> str:
        if self._current_account_ref:
            return self._current_account_ref
        raise ProbeError("probe_account_unavailable", "没有可恢复的Probe账号。")

    def _partition_for_current_seat(self) -> str:
        if not self._current_seat_id:
            raise ProbeError("probe_seat_binding_missing", "Probe座位绑定缺失。")
        seat = self._live_seats.get(self._current_seat_id)
        if seat is None:
            raise ProbeError("probe_seat_binding_missing", "Probe座位绑定缺失。")
        return f"{seat.area_code}-{seat.seat_id}"

    def _show_id_hint(self) -> str:
        # The provider receives the show id on create/status, but the activity
        # endpoint only needs the same id. It is set by create for this run.
        if not self._current_show_id:
            raise ProbeError("probe_show_binding_missing", "Probe场次绑定缺失。")
        return self._current_show_id
