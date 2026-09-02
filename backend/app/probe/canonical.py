from __future__ import annotations

from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from .models import ProbeSeatTypePrice


class CreateOrderResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: int | str | None = None
    biz_code: int | str | None = None
    temporary_order_id: str | None = Field(default=None, max_length=240)
    outcome: Literal["CONFIRMED", "FAILED", "UNKNOWN"] = "CONFIRMED"


class OrderStatusResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_status: int | str | None = None
    lock_seat_time: int | None = None


class ActivityOffersResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    able: bool
    name: str = Field(default="", max_length=240)
    total_pay_price_cents: int | None = Field(default=None, gt=0, le=2_000_000)
    member_price_cents: int | None = Field(default=None, gt=0, le=2_000_000)
    seat_type_prices: list[ProbeSeatTypePrice] = Field(default_factory=list, max_length=100)


class CancelResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool


class SeatAvailabilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available_seat_ids: set[str] = Field(default_factory=set, max_length=100)


def canonical_create_order(value: Mapping[str, Any]) -> CreateOrderResult:
    data = value.get("data") if isinstance(value.get("data"), Mapping) else value
    reference = data.get("orderId") or data.get("order_id") or data.get("temporary_order_id")
    return CreateOrderResult(
        code=value.get("code"),
        biz_code=data.get("bizCode", data.get("biz_code")) if isinstance(data, Mapping) else None,
        temporary_order_id=str(reference).strip() if reference is not None and str(reference).strip() else None,
    )


def canonical_order_status(value: Mapping[str, Any]) -> OrderStatusResult:
    return OrderStatusResult(
        order_status=value.get("orderStatus", value.get("order_status")),
        lock_seat_time=value.get("lockSeatTime", value.get("lock_seat_time")),
    )


def canonical_activity(value: Mapping[str, Any]) -> ActivityOffersResult:
    allot = value.get("allotSeat") if isinstance(value.get("allotSeat"), Mapping) else {}
    price = allot.get("totalPayPrice", value.get("totalPayPrice"))
    return ActivityOffersResult(
        able=value.get("able") is True,
        name=str(value.get("name") or ""),
        total_pay_price_cents=int(price) if isinstance(price, int) and price > 0 else None,
        member_price_cents=(int(value["member_price_cents"]) if isinstance(value.get("member_price_cents"), int) and value["member_price_cents"] > 0 else None),
        seat_type_prices=value.get("seat_type_prices", []),
    )
