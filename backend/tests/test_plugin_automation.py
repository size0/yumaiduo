from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import pytest

from app.errors import ProviderError, RecognitionError
from app.models import MovieImageInfo, RealQuote
from app.plugin_automation import (
    PluginAutomation,
    SecureImageLoader,
    _apply_declared_ticket_count,
    _authoritative_order_amount_cents,
    _cinema_venue_hint,
    _explicit_quote_confirmation,
    _explicit_seats_in_buyer_hint,
    build_action_result_decision,
    _is_new_flow_transaction,
    validate_image_url,
)
from app.reply_template_store import ReplyTemplates


@dataclass
class FakeRecognizer:
    result: MovieImageInfo
    calls: int = 0

    async def recognize(self, image: bytes, content_type: str, buyer_message: str = "", *, prior_recognitions=None):
        self.calls += 1
        assert image == b"image"
        assert content_type == "image/jpeg"
        return self.result


@dataclass
class FakeQuoter:
    result: RealQuote
    calls: int = 0

    async def quote(self, recognition: MovieImageInfo) -> RealQuote:
        self.calls += 1
        return self.result


async def load_image(_: str) -> tuple[bytes, str]:
    return b"image", "image/jpeg"


def test_new_flow_is_not_authorized_by_legacy_price_created_order() -> None:
    envelope = {
        "id": "event-new-flow",
        "event": "order.created",
        "payload": {"flow_version": "V4_NEW_FLOW_V2"},
    }
    engine = PluginAutomation(object(), object())

    assert _is_new_flow_transaction(envelope, {"platform_order_id": "order-1"}) is True
    result = asyncio.run(engine._price_created_order(
        envelope,
        {"tenant_id": "tenant-1", "shop_id": "shop-1", "buyer_id": "buyer-1", "chat_id": "chat-1", "order_id": "order-1", "event_id": "event-new-flow"},
        [],
        {"platform_order_id": "order-1", "flow_version": "V4_NEW_FLOW_V2"},
    ))
    assert result["decision"]["actions"] == []
    assert result["decision"]["reason"] == "new_flow_reprice_delegated_to_phase_9a"


def test_combined_buyer_sentence_separates_cinema_identity_and_seats() -> None:
    value = "佛山南海万达9排17、18有吗"

    assert _cinema_venue_hint(value, "佛山") == "佛山南海万达"
    assert _explicit_seats_in_buyer_hint(value) == ("9排17座", "9排18座")


def test_cinema_hint_cleaning_does_not_invent_a_venue_from_city_only_text() -> None:
    assert _cinema_venue_hint("佛山", "佛山") is None
    assert _cinema_venue_hint("佛山有吗", "佛山") is None
    assert _cinema_venue_hint("济南高新万达", "济南") == "济南高新万达"


def test_seat_hint_parser_rejects_out_of_range_coordinates() -> None:
    assert _explicit_seats_in_buyer_hint("佛山南海万达99排999座") == ()


def test_authoritative_order_amount_accepts_platform_digit_strings() -> None:
    assert _authoritative_order_amount_cents({"payment": "2000"}) == 2_000
    assert _authoritative_order_amount_cents({"payment": "20.00"}) is None
    assert _authoritative_order_amount_cents({"payment": True}) is None


def test_declared_ticket_count_recomputes_area_quote_totals() -> None:
    quote = RealQuote(
        quote_scope="area_preview",
        seat_zone_type="W+",
        seat_type="wplus",
        base_unit_cents=4_600,
        unit_quote_cents=4_890,
        ticket_count=None,
        needs_ticket_count=True,
    )

    updated = _apply_declared_ticket_count(quote, 2)

    assert updated is not None
    assert updated.ticket_count == 2
    assert updated.needs_ticket_count is False
    assert updated.base_total_cents == 9_200
    assert updated.total_quote_cents == 9_780


@pytest.mark.asyncio
async def test_secure_image_loader_retries_a_new_cdn_image_until_available() -> None:
    statuses = [404, 503, 404, 200]

    async def handler(request: httpx.Request) -> httpx.Response:
        assert "Mozilla/5.0" in request.headers["user-agent"]
        assert request.headers["referer"] == "https://www.goofish.com/"
        status = statuses.pop(0)
        return httpx.Response(
            status,
            headers={"content-type": "image/jpeg"},
            content=b"\xff\xd8\xffimage" if status == 200 else b"",
        )

    loader = SecureImageLoader(
        transport=httpx.MockTransport(handler),
        timeout_seconds=1,
        retry_delays=(0, 0, 0),
    )
    try:
        body, content_type = await loader("https://img.alicdn.com/new-image.jpg")
    finally:
        await loader.aclose()

    assert body == b"\xff\xd8\xffimage"
    assert content_type == "image/jpeg"
    assert statuses == []


def recognition() -> MovieImageInfo:
    return MovieImageInfo.model_validate({
        "movie_name": "奥德赛",
        "cinema_name": "万达影城（金平万达广场IMAX店）",
        "date_text": "今天 08月25日",
        "date": "2026-08-25",
        "showtime_start": "13:33",
        "showtime_end": "16:20",
        "hall_name": "IMAX厅",
        "selected_seats": [{"seat_number": "11排16座"}, {"seat_number": "11排15座"}],
        "selected_count_visible": 2,
        "confidence": 0.95,
    })


def exact_quote() -> RealQuote:
    return RealQuote(
        quote_scope="exact_seats",
        seat_zone_type="W+",
        total_quote_cents=11660,
        ticket_count=2,
        needs_ticket_count=False,
        pricing_source="万达官方会员价 + 后台报价规则",
        pricing_rule_version="pricing-r1-test",
        matched_cinema_name="万达影城（金平万达广场IMAX店）",
    )


def event_body(*, event: str = "order.created", session: dict | None = None, order: dict | None = None) -> dict:
    session = session if session is not None else {"accountUnb": "shop-1", "chatId": "chat-1", "peerUnb": "buyer-1"}
    order = order if order is not None else {
        "orderId": "order-1", "tenantId": "tenant-1", "accountUnb": "shop-1",
        "buyerUnb": "buyer-1", "chatId": "chat-1", "payment": 13980, "orderStatus": "created",
    }
    return {
        "envelope": {
            "id": "event-1", "tenantId": "tenant-1", "event": event, "timestamp": 1787580000000,
            "payload": {"orderId": "order-1", "accountUnb": "shop-1", "chatId": "chat-1", "peerUnb": "buyer-1"},
        },
        "session": session,
        "order": order,
        "recent_messages": [
            {
                "direction": "inbound", "messageType": 2, "imageUrls": ["https://img.alicdn.com/ticket.jpg"],
                "sentAtMs": 1787579900000, "content": "[图片]",
            }
        ],
    }


@pytest.mark.asyncio
async def test_official_showtime_mismatch_rechecks_image_before_returning_no_quote() -> None:
    class RetryRecognizer:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def recognize(self, image: bytes, content_type: str, buyer_message: str = "", *, prior_recognitions=None):
            self.messages.append(buyer_message)
            return recognition().model_copy(update={
                "showtime_start": "15:33" if len(self.messages) == 1 else "15:20",
            })

    class RetryQuoter:
        def __init__(self) -> None:
            self.calls = 0

        async def quote(self, value: MovieImageInfo) -> RealQuote:
            self.calls += 1
            if value.showtime_start != "15:20":
                raise RecognitionError(
                    "candidate_mismatch", "候选影院均没有唯一匹配截图中的影片、日期和开场时间。",
                    status_code=422,
                )
            return exact_quote()

    recognizer = RetryRecognizer()
    quoter = RetryQuoter()
    automation = PluginAutomation(recognizer, quoter, mode="auto", image_loader=load_image)

    result = await automation.process_event(event_body(event="im.message.received"))

    assert result["decision"]["actions"][0]["type"] == "send_message"
    assert quoter.calls == 2
    assert recognizer.messages[0] == ""
    assert "手机状态栏时间" in recognizer.messages[1]


@pytest.mark.asyncio
async def test_order_created_without_confirmed_quote_never_requotes_recent_image() -> None:
    recognizer = FakeRecognizer(recognition())
    quoter = FakeQuoter(exact_quote())
    automation = PluginAutomation(recognizer, quoter, mode="auto", image_loader=load_image)

    first = await automation.process_event(event_body())
    second = await automation.process_event(event_body())

    assert first == second
    assert first["decision"]["mode"] == "auto"
    assert first["decision"]["reason"] == "confirmed_quote_unavailable"
    action = first["decision"]["actions"][0]
    assert action["type"] == "guard_unverified_order"
    assert action["dedupe_key"] == "order-1:guard-unverified-order"
    assert recognizer.calls == 0
    assert quoter.calls == 0


@pytest.mark.asyncio
async def test_undelivered_confirmed_quote_cannot_authorize_order_price_change() -> None:
    body = event_body()
    durable = {
        "record_id": "quote-undelivered", "status": "succeeded", "item_id": "item-1",
        "quote_scope": "area_preview", "seat_zone_type": "W+", "unit_quote_cents": 5_000,
        "confirmed_ticket_count": 1, "confirmation_version": "v4c-undelivered",
        "quote_expires_at": "2099-08-26T05:00:00+00:00",
    }
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, quote_finder=lambda **_: durable,
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "confirmed_quote_unavailable"
    assert result["decision"]["actions"][0]["type"] == "guard_unverified_order"


@pytest.mark.asyncio
async def test_quote_bound_to_a_terminal_order_cannot_authorize_a_different_new_order() -> None:
    previous = {
        "record_id": "quote-old-order", "quote_scope": "exact_seats",
        "total_quote_cents": 8_800, "seat_display": "9排17座、9排18座",
        "confirmation_version": "v4c-old-order", "confirmation_source": "buyer_message",
        "quote_expires_at": "2026-08-26T05:00:00+00:00", "confirmed_ticket_count": 2,
        "order_id": "order-refunded",
    }
    bind_calls: list[dict[str, object]] = []
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, quote_finder=lambda **_: previous,
        quote_binder=lambda **values: bind_calls.append(values),
        quote_order_confirmer=lambda **_: None,
    )

    new_order = event_body(order={
        "orderId": "order-new", "tenantId": "tenant-1", "accountUnb": "shop-1",
        "buyerUnb": "buyer-1", "chatId": "chat-1", "itemId": "item-1",
        "payment": 2_000, "orderStatus": 1,
    })
    new_order["envelope"]["payload"]["orderId"] = "order-new"

    result = await automation.process_event(new_order)

    assert result["decision"]["reason"] == "confirmed_quote_unavailable"
    assert result["decision"]["actions"][0]["type"] == "guard_unverified_order"
    assert bind_calls == []


@pytest.mark.asyncio
async def test_later_order_uses_quote_ticket_count_not_listing_quantity_before_price_change() -> None:
    body = event_body(order={
        "orderId": "order-1", "tenantId": "tenant-1", "accountUnb": "shop-1",
        "buyerUnb": "buyer-1", "chatId": "chat-1", "itemId": "item-1",
        "payment": 13_980, "orderStatus": "created", "quantity": 1,
    })
    body["envelope"]["payload"]["itemId"] = "item-1"
    created_at = datetime.fromtimestamp(1787579900000 / 1000, tz=timezone.utc).isoformat()
    candidate = {
        "record_id": "quote-delivered", "created_at": created_at,
        "delivery_state": "delivered", "quote_scope": "exact_seats",
        "ticket_count": 2, "total_quote_cents": 11_660,
        "seat_display": "11排16座、11排15座",
    }
    confirmations: list[dict[str, object]] = []

    def find_quote(*, confirmed: bool = False, **_: object):
        return None if confirmed else candidate

    def confirm_order(**values: object):
        confirmations.append(values)
        return {
            **candidate, "confirmation_version": "v4c-order-created",
            "confirmation_source": "order_created", "quote_expires_at": "2026-08-26T05:00:00+00:00",
            "confirmed_ticket_count": 2, "order_id": "order-1",
        }

    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, quote_finder=find_quote,
        quote_order_confirmer=confirm_order,
    )

    result = await automation.process_event(body)

    assert confirmations and confirmations[0]["ticket_count"] == 2
    action = result["decision"]["actions"][0]
    assert action["type"] == "change_order_price"
    assert action["quote_snapshot"]["confirmation_source"] == "order_created"
    assert action["quote_snapshot"]["quote_record_id"] == "quote-delivered"


@pytest.mark.asyncio
async def test_listing_quantity_cannot_supply_missing_confirmed_ticket_count() -> None:
    body = event_body(order={
        "orderId": "order-1", "tenantId": "tenant-1", "accountUnb": "shop-1",
        "buyerUnb": "buyer-1", "chatId": "chat-1", "itemId": "item-1",
        "payment": 13_980, "orderStatus": "created", "quantity": 1,
    })
    body["envelope"]["payload"]["itemId"] = "item-1"
    created_at = datetime.fromtimestamp(1787579900000 / 1000, tz=timezone.utc).isoformat()
    candidate = {
        "record_id": "quote-explicit", "created_at": created_at,
        "delivery_state": "delivered", "quote_scope": "area_preview",
        "seat_zone_type": "W+", "ticket_count": None, "unit_quote_cents": 4_400,
        "confirmation_version": "v4c-without-count", "confirmation_source": "buyer_message",
    }
    confirmations: list[dict[str, object]] = []

    def find_quote(*, confirmed: bool = False, **_: object):
        return None if confirmed else candidate

    def confirm_order(**values: object):
        confirmations.append(values)
        return {
            **candidate, "confirmation_version": "v4c-with-order-count",
            "confirmation_source": "buyer_message", "quote_expires_at": "2026-08-26T05:00:00+00:00",
            "confirmed_ticket_count": 2, "order_id": "order-1",
        }

    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, quote_finder=find_quote,
        quote_order_confirmer=confirm_order,
    )

    result = await automation.process_event(body)

    assert confirmations == []
    assert result["decision"]["reason"] == "confirmed_quote_unavailable"
    assert result["decision"]["actions"][0]["type"] == "guard_unverified_order"


@pytest.mark.asyncio
async def test_automation_is_inert_when_mode_is_off() -> None:
    recognizer = FakeRecognizer(recognition())
    automation = PluginAutomation(recognizer, FakeQuoter(exact_quote()), mode="off", image_loader=load_image)

    result = await automation.process_event(event_body())

    assert result["decision"]["actions"] == []
    assert recognizer.calls == 0


@pytest.mark.asyncio
async def test_ai_disabled_image_uses_structured_text_fallback_without_calling_vision() -> None:
    recognizer = FakeRecognizer(recognition())
    automation = PluginAutomation(
        recognizer, FakeQuoter(exact_quote()), mode="auto", image_loader=load_image,
        ai_assist_enabled=False,
    )

    result = await automation.process_event(event_body(event="im.message.received"))

    assert recognizer.calls == 0
    assert result["decision"]["reason"] == "ai_assist_disabled_structured_intake_ready"
    assert "城市、影院、影片、日期、场次、影厅、座位、张数" in result["decision"]["actions"][0]["text"]


@pytest.mark.asyncio
async def test_ai_disabled_structured_text_still_reaches_authoritative_quote() -> None:
    body = event_body(event="im.message.received")
    body["order"] = None
    content = (
        "城市：深圳；影院：深圳龙岗万达影城；影片：奥德赛；日期：今天；"
        "场次：22:40；影厅：IMAX厅；座位：8排5座、8排6座；张数：2张"
    )
    body["envelope"]["payload"].update({
        "messageType": 1, "content": content, "remoteMessageId": "structured-current",
    })
    body["recent_messages"] = [{
        "direction": "inbound", "messageType": 1, "content": content,
        "messageId": "structured-current", "sentAtMs": 1787579999000,
    }]
    quoter = FakeQuoter(exact_quote())
    automation = PluginAutomation(
        FakeRecognizer(recognition()), quoter, mode="auto", image_loader=load_image,
        ai_assist_enabled=False,
    )

    result = await automation.process_event(body)

    assert quoter.calls == 1
    assert result["decision"]["reason"] == "structured_text_quote_ready"
    assert "116.60" in result["decision"]["actions"][0]["text"]
    assert "¥" not in result["decision"]["actions"][0]["text"]


@pytest.mark.asyncio
async def test_inbound_image_builds_safe_reply_action() -> None:
    quote_records: list[dict[str, object]] = []
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, quote_recorder=lambda value: quote_records.append(dict(value)),
    )

    result = await automation.process_event(event_body(event="im.message.received"))

    action = result["decision"]["actions"][0]
    assert action["type"] == "send_message"
    assert action["id"] == "event-1:reply"
    assert action["preserve_on_new_buyer_message"] is True
    assert action["suppress_on_newer_image"] is True
    assert "116.60" in action["text"]
    assert "¥" not in action["text"]
    assert "截图" not in action["text"] or "截图金额" not in action["text"]
    assert quote_records[0]["record_id"] == "event-1"
    assert quote_records[0]["tenant_id"] == "tenant-1"
    assert quote_records[0]["total_quote_cents"] == 11660
    assert quote_records[0]["source"] == "buyer_image"
    assert quote_records[0]["order_id"] is None
    assert quote_records[0]["date_text"] == "今天 08月25日"
    assert quote_records[0]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_pending_order_transaction_screenshot_gets_order_specific_safe_guidance() -> None:
    class FailingQuoter:
        async def quote(self, _: MovieImageInfo) -> RealQuote:
            raise ProviderError("wanda_quote_identity_incomplete", "直接查询万达需要完整的影片、日期和开场时间。")

    empty = MovieImageInfo(
        selected_count_visible=0, confidence=0.1,
        missing_fields=[
            "platform", "cinema_name", "city", "movie_name", "date_text", "date",
            "showtime_start", "showtime_end", "hall_name", "language", "format", "displayed_total",
        ],
    )
    body = event_body(event="im.message.received", order={
        "orderId": "pending-order-1", "tenantId": "tenant-1", "accountUnb": "shop-1",
        "buyerUnb": "buyer-1", "chatId": "chat-1", "itemId": "item-1",
        "payment": 2_000, "orderStatus": "created", "quantity": 1,
    })
    body["envelope"]["payload"].pop("orderId", None)
    body["envelope"]["payload"].update({
        "itemId": "item-1", "messageType": 2, "remoteMessageId": "transaction-shot",
        "imageUrls": ["https://img.alicdn.com/order.jpg"], "content": "[图片]",
    })
    body["recent_messages"] = [{
        "direction": "inbound", "messageType": 2, "messageId": "transaction-shot",
        "imageUrls": ["https://img.alicdn.com/order.jpg"], "content": "[图片]", "sentAtMs": 1787579999000,
    }]
    templates = ReplyTemplates(
        order_pending_without_quote_template="统一提示：暂无有效报价，请先不要付款并重新发送当前场次截图。",
    )
    automation = PluginAutomation(
        FakeRecognizer(empty), FailingQuoter(), mode="auto", image_loader=load_image,
        template_provider=lambda: templates,
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "confirmed_quote_unavailable"
    text = result["decision"]["actions"][0]["text"]
    assert text == "统一提示：暂无有效报价，请先不要付款并重新发送当前场次截图。"
    assert "platform" not in text
    assert "displayed_total" not in text


@pytest.mark.asyncio
async def test_quote_failure_is_recorded_for_operators_but_not_exposed_to_buyer() -> None:
    class FailingQuoter:
        async def quote(self, _: MovieImageInfo) -> RealQuote:
            raise ProviderError(
                "wanda_temporary_lock_release_unverified",
                "临时锁座已取消，但未确认座位恢复可售，本次不返回报价。",
            )

    quote_records: list[dict[str, object]] = []
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FailingQuoter(), mode="auto",
        image_loader=load_image, quote_recorder=lambda value: quote_records.append(dict(value)),
    )

    result = await automation.process_event(event_body(event="im.message.received"))

    text = result["decision"]["actions"][0]["text"]
    assert "未确认座位恢复" not in text
    assert "临时锁座" not in text
    assert len(quote_records) == 1
    failure = quote_records[0]
    assert failure["record_id"] == "event-1"
    assert failure["tenant_id"] == "tenant-1"
    assert failure["status"] == "failed"
    assert failure["source"] == "buyer_image"
    assert failure["cinema"] == "万达影城（金平万达广场IMAX店）"
    assert failure["seat_display"] == "11排16座、11排15座"
    assert failure["failure_reason"] == "临时锁座已取消，但未确认座位恢复可售，本次不返回报价。"


class FakeChat:
    def __init__(self) -> None:
        self.synchronized: tuple[str, list[object], str | None] | None = None

    def sync_platform_history(
        self,
        conversation_id: str,
        messages: list[object],
        *,
        current_message_id: str | None = None,
        reference_time_ms: int | None = None,
    ) -> None:
        self.synchronized = (conversation_id, messages, current_message_id)

    async def reply(self, text: str, conversation_id: str) -> str:
        assert text == "请问多少钱"
        assert conversation_id == "tenant-1:shop-1:chat-1"
        return "请把当前场次和选座截图发给我，我帮您核价。"


@pytest.mark.asyncio
async def test_canonical_shop_text_is_fenced_from_legacy_generic_ai() -> None:
    class CanaryShops:
        def is_enabled(self, tenant_id: str, shop_id: str) -> bool:
            assert (tenant_id, shop_id) == ("tenant-1", "shop-1")
            return True

        def is_canonical_quote_enabled(self, tenant_id: str, shop_id: str) -> bool:
            assert (tenant_id, shop_id) == ("tenant-1", "shop-1")
            return True

        def is_canonical_conversation_enabled(self, tenant_id: str, shop_id: str) -> bool:
            assert (tenant_id, shop_id) == ("tenant-1", "shop-1")
            return True

    body = event_body(event="im.message.received")
    body["order"] = None
    body["envelope"]["payload"].update({
        "messageType": 1,
        "remoteMessageId": "canary-current",
        "content": "请问多少钱",
        "imageUrls": [],
    })
    body["recent_messages"] = [{
        "direction": "inbound",
        "messageType": 1,
        "content": "请问多少钱",
        "messageId": "canary-current",
        "sentAtMs": 1787580000000,
        "imageUrls": [],
    }]

    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=FakeChat(),
        shop_store=CanaryShops(),
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "canonical_shop_legacy_text_fenced"
    assert result["decision"]["actions"] == []


@pytest.mark.asyncio
async def test_canonical_shop_image_is_fenced_from_legacy_recognition_and_quote() -> None:
    class CanaryShops:
        def is_enabled(self, tenant_id: str, shop_id: str) -> bool:
            assert (tenant_id, shop_id) == ("tenant-1", "shop-1")
            return True

        def is_canonical_quote_enabled(self, tenant_id: str, shop_id: str) -> bool:
            assert (tenant_id, shop_id) == ("tenant-1", "shop-1")
            return True

        def is_canonical_conversation_enabled(self, tenant_id: str, shop_id: str) -> bool:
            assert (tenant_id, shop_id) == ("tenant-1", "shop-1")
            return True

    body = event_body(event="im.message.received")
    body["order"] = None
    body["envelope"]["payload"].update({
        "messageType": 2,
        "remoteMessageId": "canary-image-current",
        "content": "[图片]",
        "imageUrls": ["https://img.alicdn.com/ticket.jpg"],
    })
    body["recent_messages"] = [{
        "direction": "inbound",
        "messageType": 2,
        "content": "[图片]",
        "messageId": "canary-image-current",
        "sentAtMs": 1787580000000,
        "imageUrls": ["https://img.alicdn.com/ticket.jpg"],
    }]
    recognizer = FakeRecognizer(recognition())
    quoter = FakeQuoter(exact_quote())
    automation = PluginAutomation(
        recognizer, quoter, mode="auto", image_loader=load_image,
        chat_service=FakeChat(), shop_store=CanaryShops(),
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "canonical_shop_legacy_image_fenced"
    assert result["decision"]["actions"] == []
    assert recognizer.calls == 0
    assert quoter.calls == 0


@pytest.mark.asyncio
async def test_fresh_quote_prevents_an_old_completed_order_from_answering_ok_as_fulfilled() -> None:
    body = event_body(event="im.message.received")
    body["envelope"]["payload"].update({
        "messageType": 1, "remoteMessageId": "ok-current", "content": "OK", "imageUrls": [],
    })
    body["order"]["orderStatus"] = 4
    body["order"]["orderId"] = "old-completed-order"
    body["envelope"]["payload"]["orderId"] = "old-completed-order"
    body["recent_messages"] = [
        {
            "direction": "outbound", "messageType": 1,
            "content": "W+报价：¥57.10/张", "messageId": "fresh-quote",
            "sentAtMs": 1787579990000, "imageUrls": [], "agent_generated": True,
        },
        {
            "direction": "inbound", "messageType": 1, "content": "OK",
            "messageId": "ok-current", "sentAtMs": 1787579999000, "imageUrls": [],
        },
    ]
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto", image_loader=load_image,
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] in {
        "quote_ticket_count_request_ready", "order_submission_guidance_ready",
    }
    assert "确认报价" not in result["decision"]["actions"][0]["text"]
    assert "需要几张" in result["decision"]["actions"][0]["text"]
    assert "出票成功" not in result["decision"]["actions"][0]["text"]


@pytest.mark.asyncio
async def test_bare_acknowledgement_without_active_transaction_does_not_call_generic_ai() -> None:
    class UnexpectedChat:
        def sync_platform_history(self, *_args, **_kwargs) -> None:
            return None

        async def reply(self, *_args, **_kwargs) -> str:
            raise AssertionError("bare acknowledgement must not reach generic AI")

    body = event_body(event="im.message.received")
    body["order"] = None
    body["envelope"]["payload"].update({
        "messageType": 1, "remoteMessageId": "ack-current", "content": "嗯嗯", "imageUrls": [],
    })
    body["recent_messages"] = [{
        "direction": "inbound", "messageType": 1, "content": "嗯嗯",
        "messageId": "ack-current", "sentAtMs": 1787579999000, "imageUrls": [],
    }]
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=UnexpectedChat(),
    )

    result = await automation.process_event(body)

    assert result["decision"] == {"mode": "auto", "actions": [], "reason": "acknowledgement_no_reply"}


@pytest.mark.asyncio
async def test_generic_ai_transaction_claim_is_rejected_without_authoritative_state() -> None:
    class UnsafeChat:
        def sync_platform_history(self, *_args, **_kwargs) -> None:
            return None

        async def reply(self, _text: str, _conversation_id: str) -> str:
            return "出票成功！票码已发，请注意查收。"

    body = event_body(event="im.message.received")
    body["order"] = None
    body["envelope"]["payload"].update({
        "messageType": 1, "remoteMessageId": "technical-current",
        "content": "你用的什么技术？", "imageUrls": [],
    })
    body["recent_messages"] = [{
        "direction": "inbound", "messageType": 1, "content": "你用的什么技术？",
        "messageId": "technical-current", "sentAtMs": 1787579999000, "imageUrls": [],
    }]
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=UnsafeChat(),
    )

    result = await automation.process_event(body)

    assert result["decision"] == {
        "mode": "auto", "actions": [], "reason": "generic_ai_transaction_claim_rejected",
    }


@pytest.mark.asyncio
async def test_generic_ai_numeric_price_claim_is_rejected_without_authoritative_quote() -> None:
    class UnsafePriceChat:
        def sync_platform_history(self, *_args, **_kwargs) -> None:
            return None

        async def reply(self, _text: str, _conversation_id: str) -> str:
            return "是的，这边按63一张给您代订。"

    body = event_body(event="im.message.received")
    body["order"] = None
    body["envelope"]["payload"].update({
        "messageType": 1, "remoteMessageId": "price-current",
        "content": "一个价啊", "imageUrls": [],
    })
    body["recent_messages"] = [{
        "direction": "inbound", "messageType": 1, "content": "一个价啊",
        "messageId": "price-current", "sentAtMs": 1787579999000, "imageUrls": [],
    }]
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=UnsafePriceChat(),
    )

    result = await automation.process_event(body)

    assert result["decision"] == {
        "mode": "auto", "actions": [], "reason": "generic_ai_transaction_claim_rejected",
    }


@pytest.mark.asyncio
async def test_current_webhook_message_wins_when_platform_history_has_not_propagated_yet() -> None:
    body = event_body(event="im.message.received")
    body["envelope"]["payload"].update({
        "messageType": 1,
        "remoteMessageId": "current-not-in-history",
        "content": "请问多少钱",
        "imageUrls": [],
    })
    body["recent_messages"] = [{
        "direction": "inbound", "messageType": 2, "content": "[旧图片]",
        "messageId": "older-image", "sentAtMs": 1787579900000,
        "imageUrls": ["https://img.alicdn.com/older.jpg"],
    }]
    recognizer = FakeRecognizer(recognition())
    chat = FakeChat()
    automation = PluginAutomation(
        recognizer, FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=chat,
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "automatic_reply_ready"
    assert result["decision"]["actions"][0]["text"] == "请把当前场次和选座截图发给我，我帮您核价。"
    assert recognizer.calls == 0
    assert chat.synchronized is not None
    assert chat.synchronized[2] == "current-not-in-history"


@pytest.mark.asyncio
async def test_simple_greeting_uses_guidance_without_hallucinated_old_context() -> None:
    body = event_body(event="im.message.received")
    body["envelope"]["payload"].update({"messageType": 1, "remoteMessageId": "greeting-current"})
    body["recent_messages"] = [{
        "direction": "inbound", "messageType": 1, "content": "在么",
        "messageId": "greeting-current", "sentAtMs": 1787579999000, "imageUrls": [],
    }]
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=FakeChat(),
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "guidance_reply_ready"
    assert result["decision"]["actions"][0]["text"] == ReplyTemplates().guidance_template
    assert "人工核对" not in result["decision"]["actions"][0]["text"]


@pytest.mark.asyncio
async def test_custom_keyword_reply_bypasses_gpt_with_deterministic_priority() -> None:
    body = event_body(event="im.message.received")
    body["envelope"]["payload"].update({"messageType": 1, "remoteMessageId": "keyword-current"})
    body["recent_messages"] = [{
        "direction": "inbound", "messageType": 1, "content": "请问W+怎么买？",
        "messageId": "keyword-current", "sentAtMs": 1787579999000, "imageUrls": [],
    }]
    templates = ReplyTemplates.model_validate({
        "keyword_replies": [{
            "id": "wplus", "keywords": ["w+怎么买"], "match_mode": "contains",
            "reply": "支持万达W+座位代订，请发送截图。", "priority": 200,
        }],
    })
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=FakeChat(), template_provider=lambda: templates,
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "custom_keyword_reply_ready"
    assert result["decision"]["actions"][0]["text"] == "支持万达W+座位代订，请发送截图。"


@pytest.mark.asyncio
async def test_keyword_reply_can_send_tenant_bound_uploaded_image() -> None:
    body = event_body(event="im.message.received")
    body["order"] = None
    body["envelope"]["payload"].update({
        "messageType": 1, "remoteMessageId": "keyword-image-current", "content": "教程",
    })
    body["recent_messages"] = [{
        "direction": "inbound", "messageType": 1, "content": "教程",
        "messageId": "keyword-image-current", "sentAtMs": 1787579999000, "imageUrls": [],
    }]
    templates = ReplyTemplates.model_validate({
        "keyword_replies": [{
            "id": "tutorial", "keywords": ["教程"], "match_mode": "exact",
            "reply": "请查看图片教程。", "image_asset_id": f"ki-{'a' * 40}",
            "image_filename": "keyword-a.png", "image_tenant_id": "tenant-1",
        }],
    })
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=FakeChat(), template_provider=lambda: templates,
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "custom_keyword_reply_ready"
    assert [action["type"] for action in result["decision"]["actions"]] == ["send_message", "send_image"]
    assert result["decision"]["actions"][1] == {
        "id": "event-1:keyword-image",
        "type": "send_image",
        "image_asset_id": f"ki-{'a' * 40}",
        "image_filename": "keyword-a.png",
        "rule_governed": True,
    }


@pytest.mark.parametrize(("message", "count"), [
    ("确认", None), ("确认报价", None), ("按报价确认", None), ("正确", None),
    ("确认2张", 2), ("按报价确认两张", 2),
    ("2张", 2), ("两张", 2), ("需要2张", 2), ("否 两张", 2),
])
def test_explicit_quote_confirmation_whitelist(message: str, count: int | None) -> None:
    assert _explicit_quote_confirmation(message) == count


@pytest.mark.parametrize("message", [
    "好的", "可以", "行", "嗯", "OK",
    "帮我买", "确认一下价格", "不确认报价",
])
def test_ambiguous_or_negative_messages_are_not_transaction_confirmation(message: str) -> None:
    assert _explicit_quote_confirmation(message) is False


@pytest.mark.asyncio
async def test_correct_uses_configured_purchase_guide_and_paired_image() -> None:
    body = event_body(event="im.message.received")
    body["order"] = None
    body["envelope"]["payload"].update({"messageType": 1, "remoteMessageId": "correct-current", "content": "正确"})
    body["recent_messages"] = [
        {"direction": "outbound", "messageType": 1, "content": "61.00/张 合计：122.00元", "messageId": "quote-1", "sentAtMs": 1787579980000, "imageUrls": []},
        {"direction": "inbound", "messageType": 1, "content": "正确", "messageId": "correct-current", "sentAtMs": 1787579999000, "imageUrls": []},
    ]
    durable = {
        "record_id": "quote-1", "status": "succeeded", "delivery_state": "delivered",
        "item_id": "item-1", "quote_scope": "exact_seats", "unit_quote_cents": 6_100,
        "total_quote_cents": 12_200, "ticket_count": 2,
    }
    templates = ReplyTemplates.model_validate({
        "keyword_replies": [{
            "id": "purchase-guide", "keywords": ["确认"], "match_mode": "contains",
            "reply": "点击右上角立即购买~确定购买，先不输密码再返回（如果有免密付款页面修改为普通支付）",
            "image_asset_id": f"ki-{'a' * 40}", "image_filename": "purchase.webp", "image_tenant_id": "tenant-1",
        }],
    })
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto", image_loader=load_image,
        template_provider=lambda: templates, quote_finder=lambda **_: durable,
        quote_confirmer=lambda **value: {**durable, "confirmed_ticket_count": value["ticket_count"]},
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "order_submission_guidance_ready"
    assert result["decision"]["actions"] == [
        {
            "id": "event-1:reply", "type": "send_message",
            "text": "点击右上角立即购买~确定购买，先不输密码再返回（如果有免密付款页面修改为普通支付）",
            "rule_governed": True,
        },
        {
            "id": "event-1:keyword-image", "type": "send_image",
            "image_asset_id": f"ki-{'a' * 40}", "image_filename": "purchase.webp", "rule_governed": True,
        },
    ]






@pytest.mark.asyncio
@pytest.mark.parametrize("buyer_message", [
    "稍等我确认一下时间 谢谢",
    "等一下，我先核对场次",
    "我看看时间，晚点回复",
])
async def test_buyer_deferral_after_quote_stays_silent_instead_of_asking_quantity(
    buyer_message: str,
) -> None:
    body = event_body(event="im.message.received")
    body["order"] = None
    body["envelope"]["payload"].update({
        "messageType": 1, "remoteMessageId": "deferral-current", "content": buyer_message,
    })
    body["recent_messages"] = [
        {"direction": "outbound", "messageType": 1, "content": "79.20/张", "messageId": "quote-1", "sentAtMs": 1787579980000},
        {"direction": "inbound", "messageType": 1, "content": buyer_message, "messageId": "deferral-current", "sentAtMs": 1787579999000},
    ]
    durable = {
        "record_id": "quote-1", "quote_scope": "area_preview",
        "unit_quote_cents": 7_920, "ticket_count": None,
    }
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=FakeChat(), quote_finder=lambda **_: durable,
    )

    result = await automation.process_event(body)

    assert result["decision"] == {
        "mode": "auto", "actions": [], "reason": "buyer_deferral_no_reply",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("buyer_message", "unit_quote_cents", "expected_count", "expected_total"), [
    ("7排中间的两张", 7_920, 2, "158.40"),
    ("3", 6_370, 3, "191.10"),
])
async def test_quantity_embedded_in_seat_preference_or_bare_prompt_answer_confirms_quote(
    buyer_message: str, unit_quote_cents: int, expected_count: int, expected_total: str,
) -> None:
    body = event_body(event="im.message.received")
    body["order"] = None
    body["envelope"]["payload"].update({
        "messageType": 1, "remoteMessageId": "count-current", "content": buyer_message,
    })
    body["recent_messages"] = [
        {"direction": "outbound", "messageType": 1, "content": "79.20/张", "messageId": "quote-1", "sentAtMs": 1787579960000},
        {"direction": "inbound", "messageType": 1, "content": "多少钱一张", "messageId": "chat-1", "sentAtMs": 1787579970000},
        {"direction": "outbound", "messageType": 1, "content": "请问需要几张？", "messageId": "prompt-1", "sentAtMs": 1787579980000},
        {"direction": "inbound", "messageType": 1, "content": buyer_message, "messageId": "count-current", "sentAtMs": 1787579999000},
    ]
    durable = {
        "record_id": "quote-1", "quote_scope": "area_preview",
        "unit_quote_cents": unit_quote_cents, "ticket_count": None,
    }
    confirmed: list[int | None] = []
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=FakeChat(),
        quote_finder=lambda **_: durable,
        quote_confirmer=lambda **value: (
            confirmed.append(value["ticket_count"])
            or {**durable, "confirmed_ticket_count": value["ticket_count"]}
        ),
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "order_submission_guidance_ready"
    assert confirmed == [expected_count]
    assert f"已确认需要{expected_count}张" in result["decision"]["actions"][0]["text"]
    assert f"合计{expected_total}元" in result["decision"]["actions"][0]["text"]
    assert "请问需要几张" not in result["decision"]["actions"][0]["text"]


@pytest.mark.asyncio
async def test_bare_number_without_immediately_preceding_count_prompt_is_not_confirmation() -> None:
    body = event_body(event="im.message.received")
    body["order"] = None
    body["envelope"]["payload"].update({
        "messageType": 1, "remoteMessageId": "bare-current", "content": "3",
    })
    body["recent_messages"] = [
        {"direction": "outbound", "messageType": 1, "content": "63.70/张", "messageId": "quote-1", "sentAtMs": 1787579980000},
        {"direction": "inbound", "messageType": 1, "content": "3", "messageId": "bare-current", "sentAtMs": 1787579999000},
    ]
    durable = {
        "record_id": "quote-1", "quote_scope": "area_preview",
        "unit_quote_cents": 6_370, "ticket_count": None,
    }
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=FakeChat(), quote_finder=lambda **_: durable,
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "quote_ticket_count_request_ready"
    assert result["decision"]["actions"][0]["text"] == "请问需要几张？"


def test_quantity_guidance_contains_no_customer_facing_hardcoded_prose() -> None:
    source = inspect.getsource(__import__(
        "app.plugin_automation", fromlist=["_quantity_order_guidance"],
    )._quantity_order_guidance)

    assert "已确认需要" not in source
    assert "人工会" not in source
    assert "请放心下单" not in source
    assert "点击右上角" not in source


@pytest.mark.asyncio
async def test_ticket_count_sends_purchase_instruction_and_guide_image() -> None:
    body = event_body(event="im.message.received")
    body["order"] = None
    body["envelope"]["payload"].update({
        "messageType": 1, "remoteMessageId": "count-current", "content": "2张",
    })
    body["recent_messages"] = [
        {"direction": "outbound", "messageType": 1, "content": "69.70/张", "messageId": "quote-1", "sentAtMs": 1787579980000},
        {"direction": "inbound", "messageType": 1, "content": "2张", "messageId": "count-current", "sentAtMs": 1787579999000},
    ]
    durable = {
        "record_id": "quote-1", "quote_scope": "area_preview",
        "unit_quote_cents": 6_970, "ticket_count": None,
    }
    templates = ReplyTemplates.model_validate({
        "quote_quantity_order_guidance_template": (
            "后台完整回复：{张数}张，单价{报价单价}，合计{报价合计}元。\n"
            "{座位说明}\n{下单引导}"
        ),
        "quote_quantity_default_seat_template": "后台配置的座位说明。",
        "order_submit_unpaid_template": "后台配置：直接提交订单，系统按{张数}张核算，先不要付款。",
        "keyword_replies": [{
            "id": "purchase-guide", "keywords": ["确认"], "match_mode": "contains",
            "reply": "点击右上角立即购买~确定购买，先不输密码再返回（如果有免密付款页面修改为普通支付）",
            "image_asset_id": f"ki-{'a' * 40}", "image_filename": "purchase-guide.webp",
            "image_tenant_id": "tenant-1", "enabled": True, "priority": 100,
        }],
    })
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=FakeChat(), template_provider=lambda: templates,
        quote_finder=lambda **_: durable,
        quote_confirmer=lambda **value: {**durable, "confirmed_ticket_count": value["ticket_count"]},
    )

    result = await automation.process_event(body)

    actions = result["decision"]["actions"]
    assert result["decision"]["reason"] == "order_submission_guidance_ready"
    assert actions[0] == {
        "id": "event-1:reply", "type": "send_message",
        "text": "点击右上角立即购买~确定购买，先不输密码再返回（如果有免密付款页面修改为普通支付）",
        "rule_governed": True,
    }
    assert actions[1] == {
        "id": "event-1:keyword-image", "type": "send_image",
        "image_asset_id": f"ki-{'a' * 40}", "image_filename": "purchase-guide.webp",
        "rule_governed": True,
    }




@pytest.mark.asyncio
async def test_emoji_acceptance_after_unit_quote_asks_for_ticket_count_during_human_handoff() -> None:
    body = event_body(event="im.message.received")
    body["order"] = None
    body["envelope"]["payload"].update({
        "messageType": 1, "remoteMessageId": "accept-current", "content": "可以👌",
    })
    body["recent_messages"] = [
        {"direction": "outbound", "messageType": 1, "content": "W+区域报价单价：¥39.50一张", "messageId": "quote-1", "sentAtMs": 1787579970000},
        {"direction": "outbound", "messageType": 1, "content": "我看一下", "messageId": "human-1", "sentAtMs": 1787579995000},
        {"direction": "inbound", "messageType": 1, "content": "可以👌", "messageId": "accept-current", "sentAtMs": 1787579999000},
    ]
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=FakeChat(),
        quote_finder=lambda **_: {
            "record_id": "quote-1", "quote_scope": "area_preview",
            "seat_zone_type": "W+", "ticket_count": None,
        },
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "quote_ticket_count_request_ready"
    assert result["decision"]["actions"] == [{
        "id": "event-1:reply", "type": "send_message",
        "text": "请问需要几张？",
        "rule_governed": True,
    }]




@pytest.mark.asyncio
async def test_expired_quote_confirmation_requests_a_fresh_screenshot_instead_of_ticket_count() -> None:
    body = event_body(event="im.message.received")
    body["order"] = None
    body["envelope"]["payload"].update({"messageType": 1, "remoteMessageId": "confirm-current"})
    body["recent_messages"] = [
        {"direction": "outbound", "messageType": 1, "content": "报价合计：¥77.80", "messageId": "quote-old", "sentAtMs": 1787576400000},
        {"direction": "inbound", "messageType": 1, "content": "确认", "messageId": "confirm-current", "sentAtMs": 1787579999000},
    ]
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=FakeChat(), quote_finder=lambda **_: None,
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "authoritative_quote_expired_reply_ready"
    assert result["decision"]["actions"][0]["text"] == ReplyTemplates().quote_expired_template
    assert "几张" not in result["decision"]["actions"][0]["text"]






@pytest.mark.asyncio
async def test_buyer_can_confirm_and_price_an_order_created_before_the_quote() -> None:
    body = event_body(event="im.message.received", order={
        "orderId": "order-before-quote", "tenantId": "tenant-1", "accountUnb": "shop-1",
        "buyerUnb": "buyer-1", "chatId": "chat-1", "itemId": "item-1",
        "payment": 13_980, "orderStatus": "created", "quantity": 1,
    })
    body["envelope"]["payload"] = {
        "accountUnb": "shop-1", "chatId": "chat-1", "peerUnb": "buyer-1",
        "itemId": "item-1", "messageType": 1, "remoteMessageId": "confirm-after-order",
        "content": "确认", "imageUrls": [],
    }
    body["recent_messages"] = [
        {"direction": "outbound", "messageType": 1, "content": "W+专享区后台报价单价：¥110.60", "messageId": "quote-1", "sentAtMs": 1787579980000},
        {"direction": "inbound", "messageType": 1, "content": "确认", "messageId": "confirm-after-order", "sentAtMs": 1787579999000},
    ]
    durable = {
        "record_id": "quote-after-order", "status": "succeeded", "item_id": "item-1",
        "delivery_state": "delivered", "quote_scope": "area_preview", "seat_zone_type": "W+", "unit_quote_cents": 11_060,
        "ticket_count": 1,
    }
    confirmations: list[dict[str, object]] = []
    bindings: list[dict[str, object]] = []

    def confirm(**value):
        confirmations.append(value)
        durable.update({
            "confirmed_ticket_count": value["ticket_count"],
            "confirmation_version": "v4c-after-order",
            "quote_expires_at": "2026-08-24T12:30:00+00:00",
        })
        return durable

    templates = ReplyTemplates.model_validate({
        "keyword_replies": [{
            "id": "confirm-help", "keywords": ["确认"], "match_mode": "contains",
            "reply": "点击右上角立即购买，先不要付款。",
        }],
    })
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=FakeChat(), template_provider=lambda: templates,
        quote_finder=lambda **_: durable,
        quote_confirmer=confirm,
        quote_binder=lambda **value: bindings.append(value) or durable,
    )

    result = await automation.process_event(body)

    assert confirmations, result
    assert confirmations[0]["ticket_count"] == 1
    assert result["decision"]["reason"] == "confirmed_quote_record_bound_to_order"
    assert result["decision"]["actions"][0]["text"] == "点击右上角立即购买，先不要付款。"
    action = result["decision"]["actions"][1]
    assert action["type"] == "change_order_price"
    assert action["quote_snapshot"]["order_id"] == "order-before-quote"
    assert action["quote_snapshot"]["target_amount_cents"] == 11_060
    assert bindings[0]["order_id"] == "order-before-quote"


@pytest.mark.asyncio
async def test_confirmed_durable_wplus_quote_ignores_listing_quantity_when_pricing_order() -> None:
    body = event_body(order={
        "orderId": "order-1", "tenantId": "tenant-1", "accountUnb": "shop-1",
        "buyerUnb": "buyer-1", "chatId": "chat-1", "itemId": "item-1",
        "payment": 13_980, "orderStatus": "created", "quantity": 1,
    })
    body["recent_messages"] = [
        {
            "direction": "inbound", "messageType": 2, "content": "[图片]",
            "messageId": "fresh-image", "sentAtMs": 1787579950000,
            "imageUrls": ["https://img.alicdn.com/ticket.jpg"],
        },
        {
            "direction": "inbound", "messageType": 26, "content": "我已拍下，待付款",
            "messageId": "pending-1", "sentAtMs": 1787579999000, "imageUrls": [],
        },
    ]
    durable = {
        "record_id": "quote-record-1", "status": "succeeded", "item_id": "item-1",
        "delivery_state": "delivered", "quote_scope": "area_probe", "seat_zone_type": "W+", "unit_quote_cents": 5990,
        "confirmed_ticket_count": 2, "confirmation_version": "v4c-confirmed",
        "quote_expires_at": "2026-08-24T12:30:00+00:00",
    }
    recognizer = FakeRecognizer(recognition())
    quoter = FakeQuoter(exact_quote())
    bindings: list[dict[str, object]] = []
    automation = PluginAutomation(
        recognizer, quoter, mode="auto", image_loader=load_image,
        quote_finder=lambda **value: durable if value.get("confirmed") else durable,
        quote_binder=lambda **value: bindings.append(value) or durable,
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "confirmed_quote_record_bound_to_order"
    action = result["decision"]["actions"][0]
    assert action["type"] == "change_order_price"
    assert action["quote_snapshot"]["target_amount_cents"] == 11_980
    assert action["quote_snapshot"]["quote_record_id"] == "quote-record-1"
    assert action["quote_snapshot"]["confirmation_version"] == "v4c-confirmed"
    assert action["quote_snapshot"]["observed_order_amount_cents"] == 13_980
    assert bindings[0]["order_id"] == "order-1"
    assert bindings[0]["record_id"] == "quote-record-1"
    assert recognizer.calls == 0
    assert quoter.calls == 0


@pytest.mark.asyncio
async def test_order_paid_event_sends_deterministic_payment_confirmation() -> None:
    body = event_body(event="order.paid")
    body["order"].update({"orderStatus": 2, "payTime": "2026-08-25T13:17:01Z"})
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=FakeChat(),
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "authoritative_payment_confirmation_ready"
    assert result["decision"]["actions"] == [{
        "id": "event-1:payment-confirmation",
        "type": "send_message",
        "order_id": "order-1",
        "text": ReplyTemplates().payment_success_pending_ticket_template,
        "preserve_on_new_buyer_message": True,
        "rule_governed": True,
    }]


@pytest.mark.asyncio
async def test_order_paid_event_fails_closed_when_authoritative_order_is_not_paid() -> None:
    body = event_body(event="order.paid")
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=FakeChat(),
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "paid_order_state_unverified"
    assert result["decision"]["actions"] == []


@pytest.mark.asyncio
async def test_paid_order_status_question_uses_authoritative_order_instead_of_gpt() -> None:
    body = event_body(
        event="im.message.received",
        order={
            "orderId": "order-1", "tenantId": "tenant-1", "accountUnb": "shop-1",
            "buyerUnb": "buyer-1", "chatId": "chat-1", "orderStatus": 2,
        },
    )
    body["envelope"]["payload"].update({"messageType": 1, "remoteMessageId": "status-current"})
    body["recent_messages"] = [{
        "direction": "inbound", "messageType": 1, "content": "OK了吗",
        "messageId": "status-current", "sentAtMs": 1787579999000, "imageUrls": [],
    }]
    chat = FakeChat()
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=chat,
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "authoritative_paid_status_reply_ready"
    assert result["decision"]["actions"][0]["text"] == ReplyTemplates().payment_success_pending_ticket_template


@pytest.mark.asyncio
async def test_shipped_order_acknowledgement_never_falls_back_to_pending_payment_gpt_reply() -> None:
    body = event_body(
        event="im.message.received",
        order={
            "orderId": "order-1", "tenantId": "tenant-1", "accountUnb": "shop-1",
            "buyerUnb": "buyer-1", "chatId": "chat-1", "orderStatus": 3,
            "payTime": "2026-08-25T10:02:30.000Z",
        },
    )
    body["envelope"]["payload"].update({"messageType": 1, "remoteMessageId": "ack-current"})
    body["recent_messages"] = [{
        "direction": "inbound", "messageType": 1, "content": "OK",
        "messageId": "ack-current", "sentAtMs": 1787579999000, "imageUrls": [],
    }]
    chat = FakeChat()
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=chat,
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "authoritative_shipped_status_reply_ready"
    assert result["decision"]["actions"][0]["text"] == ReplyTemplates().order_shipped_template


@pytest.mark.asyncio
async def test_shipped_rule_reply_survives_generic_ai_stage_gate_for_ticket_collection() -> None:
    class Policy:
        stage_gate_enabled = True
        intervention_start = "consultation"
        intervention_end = "payment"
        human_takeover_delay_seconds = 20

    body = event_body(
        event="im.message.received",
        order={
            "orderId": "order-1", "tenantId": "tenant-1", "accountUnb": "shop-1",
            "buyerUnb": "buyer-1", "chatId": "chat-1", "orderStatus": 3,
            "payTime": "2026-08-25T10:02:30.000Z",
        },
    )
    body["envelope"]["payload"].update({
        "messageType": 1, "remoteMessageId": "collection-current",
        "content": "好的 我现在去取票",
    })
    body["recent_messages"] = [{
        "direction": "inbound", "messageType": 1, "content": "好的 我现在去取票",
        "messageId": "collection-current", "sentAtMs": 1787579999000, "imageUrls": [],
    }]
    chat = FakeChat()
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=chat, conversation_policy_provider=lambda: Policy(),
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "authoritative_shipped_status_reply_ready"
    assert result["decision"]["actions"][0]["text"] == ReplyTemplates().order_shipped_template


@pytest.mark.asyncio
@pytest.mark.parametrize("buyer_text", ["OK，我核对了就确认", "谢谢"])
async def test_shipped_order_blocks_stale_confirmation_keyword_and_generic_chat_reply(
    buyer_text: str,
) -> None:
    body = event_body(
        event="im.message.received",
        order={
            "orderId": "order-1", "tenantId": "tenant-1", "accountUnb": "shop-1",
            "buyerUnb": "buyer-1", "chatId": "chat-1", "orderStatus": 3,
            "payTime": "2026-08-25T10:02:30.000Z",
        },
    )
    body["envelope"]["payload"].update({
        "messageType": 1, "remoteMessageId": "shipped-current", "content": buyer_text,
    })
    body["recent_messages"] = [{
        "direction": "inbound", "messageType": 1, "content": buyer_text,
        "messageId": "shipped-current", "sentAtMs": 1787579999000, "imageUrls": [],
    }]
    templates = ReplyTemplates.model_validate({
        "keyword_replies": [{
            "id": "confirm", "keywords": ["确认"], "match_mode": "contains",
            "reply": "不客气，您核对好后再确认；出票结果以订单更新通知为准。",
            "image_asset_id": f"ki-{'a' * 40}", "image_filename": "confirm.png",
            "image_tenant_id": "tenant-1",
        }],
    })
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=FakeChat(), template_provider=lambda: templates,
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "authoritative_shipped_status_reply_ready"
    assert result["decision"]["actions"] == [{
        "id": "event-1:reply", "type": "send_message", "text": templates.order_shipped_template,
        "rule_governed": True,
    }]


@pytest.mark.asyncio
async def test_recent_buyer_cinema_clarification_completes_a_truncated_image_name() -> None:
    class HintQuoter:
        async def complete_cinema(self, value: MovieImageInfo) -> MovieImageInfo:
            if value.cinema_name != "宁波江北万达" or "city" in value.missing_fields:
                raise ProviderError("cinema_not_unique", "影院提示必须能够参与唯一城市匹配")
            return value.model_copy(update={"cinema_name": "宁波江北万达广场店", "city": "宁波"})

        async def quote(self, value: MovieImageInfo) -> RealQuote:
            assert value.city == "宁波"
            assert value.cinema_name == "宁波江北万达广场店"
            return exact_quote().model_copy(update={"matched_cinema_name": value.cinema_name})

    body = event_body(event="im.message.received")
    body["envelope"]["payload"]["remoteMessageId"] = "image-current"
    body["recent_messages"] = [
        {"direction": "inbound", "messageType": 1, "content": "宁波江北万达", "messageId": "hint-1", "sentAtMs": 1787579990000, "imageUrls": []},
        {"direction": "inbound", "messageType": 2, "content": "[图片]", "messageId": "image-current", "sentAtMs": 1787579999000, "imageUrls": ["https://img.alicdn.com/ticket.jpg"]},
    ]
    truncated = recognition().model_copy(update={
        "cinema_name": "万达影城（江...", "city": None, "missing_fields": ["city"],
    })
    automation = PluginAutomation(FakeRecognizer(truncated), HintQuoter(), mode="auto", image_loader=load_image)

    result = await automation.process_event(body)

    reply = result["decision"]["actions"][0]["text"]
    assert "城市：宁波" in reply
    assert "影院：宁波江北万达广场店" in reply


@pytest.mark.asyncio
async def test_recent_city_venue_and_seat_question_is_cleaned_before_matching_truncated_cinema() -> None:
    class BuyerSentenceQuoter:
        async def canonical_city_hint(self, value: str) -> str | None:
            return "佛山" if value == "佛山南海万达9排17、18有吗" else None

        async def complete_cinema(self, value: MovieImageInfo) -> MovieImageInfo:
            assert value.city == "佛山"
            assert value.cinema_name == "佛山南海万达"
            assert "city" not in value.missing_fields
            return value.model_copy(update={
                "city": "佛山", "cinema_name": "佛山南海万达广场店",
            })

        async def quote(self, value: MovieImageInfo) -> RealQuote:
            assert value.city == "佛山"
            assert value.cinema_name == "佛山南海万达广场店"
            assert [seat.seat_number for seat in value.selected_seats] == ["9排17座", "9排18座"]
            assert value.selected_count_visible == 2
            return exact_quote().model_copy(update={
                "matched_city_name": value.city,
                "matched_cinema_name": value.cinema_name,
            })

    body = event_body(event="im.message.received")
    body["envelope"]["payload"]["remoteMessageId"] = "image-current"
    body["recent_messages"] = [
        {
            "direction": "inbound", "messageType": 1,
            "content": "佛山南海万达9排17、18有吗", "messageId": "hint-1",
            "sentAtMs": 1787579990000, "imageUrls": [],
        },
        {
            "direction": "inbound", "messageType": 2, "content": "[图片]",
            "messageId": "image-current", "sentAtMs": 1787579999000,
            "imageUrls": ["https://img.alicdn.com/ticket.jpg"],
        },
    ]
    truncated = recognition().model_copy(update={
        "cinema_name": "万达影城（南海万达广场I...",
        "city": None,
        "missing_fields": ["city"],
    })
    automation = PluginAutomation(
        FakeRecognizer(truncated), BuyerSentenceQuoter(), mode="auto", image_loader=load_image,
    )

    result = await automation.process_event(body)

    reply = result["decision"]["actions"][0]["text"]
    assert "城市：佛山" in reply
    assert "影院：佛山南海万达广场店" in reply


@pytest.mark.asyncio
async def test_newer_cinema_text_reprocesses_the_previous_image_as_one_context() -> None:
    class HintQuoter:
        async def complete_cinema(self, value: MovieImageInfo) -> MovieImageInfo:
            if value.cinema_name != "宁波江北万达":
                raise ProviderError("cinema_not_unique", "影院无法唯一匹配")
            return value.model_copy(update={"cinema_name": "宁波江北万达广场店", "city": "宁波"})

        async def quote(self, value: MovieImageInfo) -> RealQuote:
            return exact_quote().model_copy(update={"matched_cinema_name": value.cinema_name})

    class UnexpectedChat:
        async def reply(self, text: str, conversation_id: str) -> str:
            raise AssertionError("cinema clarification with a fresh image must not become generic chat")

    body = event_body(event="im.message.received")
    body["envelope"]["timestamp"] = 1787580000000
    body["envelope"]["payload"]["remoteMessageId"] = "cinema-hint"
    body["recent_messages"] = [
        {"direction": "inbound", "messageType": 2, "content": "[图片]", "messageId": "image-previous", "sentAtMs": 1787579990000, "imageUrls": ["https://img.alicdn.com/ticket.jpg"]},
        {"direction": "inbound", "messageType": 1, "content": "宁波江北万达", "messageId": "cinema-hint", "sentAtMs": 1787579999000, "imageUrls": []},
    ]
    truncated = recognition().model_copy(update={"cinema_name": "万达影城（江...", "city": None})
    automation = PluginAutomation(
        FakeRecognizer(truncated), HintQuoter(), mode="auto",
        image_loader=load_image, chat_service=UnexpectedChat(),
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "contextual_image_reply_ready"
    assert result["decision"]["actions"][0]["preserve_on_new_buyer_message"] is True
    reply = result["decision"]["actions"][0]["text"]
    assert "城市：宁波" in reply
    assert "影院：宁波江北万达广场店" in reply


@pytest.mark.asyncio
async def test_city_only_message_reprocesses_the_previous_image_instead_of_interrupting_it() -> None:
    class CityHintQuoter:
        async def is_known_city_hint(self, value: str) -> bool:
            return value == "南宁"

        async def complete_cinema(self, value: MovieImageInfo) -> MovieImageInfo:
            if value.city != "南宁" or "city" in value.missing_fields:
                raise ProviderError("cinema_not_unique", "影院需要已验证且不再缺失的城市")
            return value.model_copy(update={"cinema_name": "南宁安吉万达广场店", "city": "南宁"})

        async def quote(self, value: MovieImageInfo) -> RealQuote:
            return exact_quote().model_copy(update={"matched_cinema_name": value.cinema_name})

    class UnexpectedChat:
        async def reply(self, text: str, conversation_id: str) -> str:
            raise AssertionError("city clarification must continue the image workflow")

    body = event_body(event="im.message.received")
    body["envelope"]["timestamp"] = 1787580000000
    body["envelope"]["payload"]["remoteMessageId"] = "city-hint"
    body["recent_messages"] = [
        {"direction": "inbound", "messageType": 2, "content": "[图片]", "messageId": "image-previous", "sentAtMs": 1787579990000, "imageUrls": ["https://img.alicdn.com/ticket.jpg"]},
        {"direction": "inbound", "messageType": 1, "content": "南宁", "messageId": "city-hint", "sentAtMs": 1787579999000, "imageUrls": []},
    ]
    truncated = recognition().model_copy(update={
        "cinema_name": "万达影城（安吉万达广场IMAX店）",
        "city": None, "missing_fields": ["city"],
    })
    automation = PluginAutomation(
        FakeRecognizer(truncated), CityHintQuoter(), mode="auto",
        image_loader=load_image, chat_service=UnexpectedChat(),
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "contextual_image_reply_ready"
    action = result["decision"]["actions"][0]
    assert action["preserve_on_new_buyer_message"] is True
    assert "城市：南宁" in action["text"]
    assert "影院：南宁安吉万达广场店" in action["text"]


@pytest.mark.asyncio
async def test_non_identity_text_after_image_never_requotes_the_image() -> None:
    class GenericChat:
        def sync_platform_history(self, *args, **kwargs) -> None:
            pass

        async def reply(self, text: str, conversation_id: str) -> str:
            assert text == "可以么"
            return "可以，请告诉我具体想咨询什么。"

    body = event_body(event="im.message.received")
    body["order"] = None
    body["envelope"]["payload"].update({
        "messageType": 1, "remoteMessageId": "retry-image", "content": "可以么",
    })
    body["recent_messages"] = [
        {"direction": "inbound", "messageType": 2, "content": "[图片]", "messageId": "image-blocked", "sentAtMs": 1787579950000, "imageUrls": ["https://img.alicdn.com/ticket.jpg"]},
        {"direction": "inbound", "messageType": 1, "content": "可以么", "messageId": "retry-image", "sentAtMs": 1787579999000, "imageUrls": []},
    ]
    recognizer = FakeRecognizer(recognition())
    quoter = FakeQuoter(exact_quote())
    automation = PluginAutomation(
        recognizer, quoter, mode="auto", image_loader=load_image, chat_service=GenericChat(),
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "automatic_reply_ready"
    assert result["decision"]["actions"][0]["text"] == "可以，请告诉我具体想咨询什么。"
    assert recognizer.calls == 0
    assert quoter.calls == 0


@pytest.mark.asyncio
async def test_city_text_only_requotes_when_it_is_the_next_buyer_message_after_image() -> None:
    class CityQuoter(FakeQuoter):
        async def is_known_city_hint(self, value: str) -> bool:
            return value == "南宁"

    class GenericChat:
        def sync_platform_history(self, *args, **kwargs) -> None:
            pass

        async def reply(self, text: str, conversation_id: str) -> str:
            assert text == "南宁"
            return "已收到城市信息。"

    body = event_body(event="im.message.received")
    body["order"] = None
    body["envelope"]["payload"].update({
        "messageType": 1, "remoteMessageId": "city-current", "content": "南宁",
    })
    body["recent_messages"] = [
        {"direction": "inbound", "messageType": 2, "content": "[图片]", "messageId": "image-old", "sentAtMs": 1787579900000, "imageUrls": ["https://img.alicdn.com/ticket.jpg"]},
        {"direction": "inbound", "messageType": 1, "content": "帮我看看", "messageId": "text-between", "sentAtMs": 1787579950000, "imageUrls": []},
        {"direction": "inbound", "messageType": 1, "content": "南宁", "messageId": "city-current", "sentAtMs": 1787579999000, "imageUrls": []},
    ]
    recognizer = FakeRecognizer(recognition())
    automation = PluginAutomation(
        recognizer, CityQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=GenericChat(),
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "automatic_reply_ready"
    assert recognizer.calls == 0


@pytest.mark.asyncio
async def test_city_and_district_message_is_canonicalized_before_reprocessing_previous_image() -> None:
    class CityDistrictQuoter:
        async def canonical_city_hint(self, value: str) -> str | None:
            return "济南" if value == "济南历下" else None

        async def complete_cinema(self, value: MovieImageInfo) -> MovieImageInfo:
            assert value.city == "济南"
            return value.model_copy(update={"cinema_name": "济南万达影城世茂广场店", "city": "济南"})

        async def quote(self, value: MovieImageInfo) -> RealQuote:
            return exact_quote().model_copy(update={
                "matched_city_name": "济南", "matched_cinema_name": value.cinema_name,
            })

    class UnexpectedChat:
        async def reply(self, text: str, conversation_id: str) -> str:
            raise AssertionError("city+district clarification must continue the image workflow")

    body = event_body(event="im.message.received")
    body["envelope"]["payload"]["remoteMessageId"] = "city-district-hint"
    body["recent_messages"] = [
        {"direction": "inbound", "messageType": 2, "content": "[图片]", "messageId": "image-previous", "sentAtMs": 1787579990000, "imageUrls": ["https://img.alicdn.com/ticket.jpg"]},
        {"direction": "inbound", "messageType": 1, "content": "济南历下", "messageId": "city-district-hint", "sentAtMs": 1787579999000, "imageUrls": []},
    ]
    truncated = recognition().model_copy(update={"cinema_name": "万达影城（世茂杜比影院店）", "city": None})
    automation = PluginAutomation(
        FakeRecognizer(truncated), CityDistrictQuoter(), mode="auto",
        image_loader=load_image, chat_service=UnexpectedChat(),
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "contextual_image_reply_ready"
    assert "城市：济南" in result["decision"]["actions"][0]["text"]
    assert "影院：济南万达影城世茂广场店" in result["decision"]["actions"][0]["text"]


@pytest.mark.asyncio
async def test_city_plus_venue_hint_keeps_both_city_and_venue_when_reprocessing_image() -> None:
    class CityVenueQuoter:
        async def canonical_city_hint(self, value: str) -> str | None:
            return "济南" if value == "济南高新万达" else None

        async def complete_cinema(self, value: MovieImageInfo) -> MovieImageInfo:
            assert value.city == "济南"
            assert value.cinema_name == "济南高新万达"
            return value.model_copy(update={"cinema_name": "济南高新万达广场店"})

        async def quote(self, value: MovieImageInfo) -> RealQuote:
            return exact_quote().model_copy(update={
                "matched_city_name": "济南", "matched_cinema_name": value.cinema_name,
            })

    body = event_body(event="im.message.received")
    body["envelope"]["payload"]["remoteMessageId"] = "city-venue-hint"
    body["recent_messages"] = [
        {
            "direction": "inbound", "messageType": 2, "content": "[图片]",
            "messageId": "image-previous", "sentAtMs": 1787579990000,
            "imageUrls": ["https://img.alicdn.com/ticket.jpg"],
        },
        {
            "direction": "inbound", "messageType": 1, "content": "济南高新万达",
            "messageId": "city-venue-hint", "sentAtMs": 1787579999000, "imageUrls": [],
        },
    ]
    truncated = recognition().model_copy(update={
        "cinema_name": "万达影城（高新IMAX激光店）", "city": None,
    })
    automation = PluginAutomation(
        FakeRecognizer(truncated), CityVenueQuoter(), mode="auto", image_loader=load_image,
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "contextual_image_reply_ready"
    assert "城市：济南" in result["decision"]["actions"][0]["text"]
    assert "影院：济南高新万达广场店" in result["decision"]["actions"][0]["text"]


@pytest.mark.asyncio
async def test_unavailable_cdn_image_gets_editable_retry_guidance_instead_of_silence() -> None:
    async def unavailable_image(_: str) -> tuple[bytes, str]:
        raise ValueError("image_download_failed")

    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=unavailable_image,
    )

    result = await automation.process_event(event_body(event="im.message.received"))

    assert result["decision"]["reason"] == "image_recognition_failure_reply_ready"
    assert result["decision"]["actions"][0]["type"] == "send_message"
    assert "重新发送清晰、完整的当前场次与选座截图" in result["decision"]["actions"][0]["text"]


@pytest.mark.asyncio
async def test_inbound_text_builds_ai_reply_action() -> None:
    body = event_body(event="im.message.received")
    body["recent_messages"] = [{
        "direction": "inbound", "messageType": 1, "content": "请问多少钱",
        "messageId": "message-1", "sentAtMs": 1787579999000, "imageUrls": [],
    }]
    body["envelope"]["payload"]["remoteMessageId"] = "message-1"
    chat = FakeChat()
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=chat,
    )

    result = await automation.process_event(body)

    assert chat.synchronized == ("tenant-1:shop-1:chat-1", body["recent_messages"], "message-1")
    assert result["decision"]["actions"] == [{
        "id": "event-1:reply", "type": "send_message",
        "text": "请把当前场次和选座截图发给我，我帮您核价。",
    }]


@pytest.mark.asyncio
async def test_pending_order_status_warns_buyer_not_to_pay_before_price_confirmation() -> None:
    body = event_body(event="im.message.received")
    body["recent_messages"] = [{
        "direction": "inbound", "messageType": 26, "content": "我已拍下，待付款",
        "orderId": "order-1", "messageId": "status-1", "sentAtMs": 1787579999000, "imageUrls": [],
    }]
    body["envelope"]["payload"].update({"remoteMessageId": "status-1", "messageType": 26, "orderId": "order-1"})
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=FakeChat(),
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "confirmed_quote_unavailable"
    assert result["decision"]["actions"][0]["type"] == "guard_unverified_order"
    assert result["decision"]["actions"][0]["dedupe_key"] == "order-1:guard-unverified-order"
    assert "请先不要付款" in result["decision"]["actions"][0]["unpaid_text"]


@pytest.mark.asyncio
async def test_pending_order_status_never_requotes_a_recent_image_without_confirmed_quote() -> None:
    body = event_body(event="im.message.received")
    body["recent_messages"] = [
        {
            "direction": "inbound", "messageType": 2, "content": "image",
            "messageId": "image-1", "sentAtMs": 1787579998000,
            "imageUrls": ["https://img.alicdn.com/test.jpg"],
        },
        {
            "direction": "inbound", "messageType": 26, "content": "我已拍下，待付款",
            "orderId": "order-1", "messageId": "status-1", "sentAtMs": 1787579999000,
            "imageUrls": [],
        },
    ]
    body["envelope"]["payload"].update({
        "remoteMessageId": "status-1", "messageType": 26,
        "content": "我已拍下，待付款", "orderId": "order-1",
    })
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=FakeChat(),
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "confirmed_quote_unavailable"
    action = result["decision"]["actions"][0]
    assert action["type"] == "guard_unverified_order"
    assert action["dedupe_key"] == "order-1:guard-unverified-order"


@pytest.mark.asyncio
async def test_paid_platform_transaction_status_sends_fixed_pending_ticket_reply() -> None:
    content = "我已付款，等待你发货"
    class UnexpectedChat:
        async def reply(self, text: str, conversation_id: str) -> str:
            raise AssertionError("platform transaction status must not enter AI chat")

    body = event_body(event="im.message.received", order={
        "orderId": "order-1", "tenantId": "tenant-1", "accountUnb": "shop-1",
        "buyerUnb": "buyer-1", "chatId": "chat-1", "orderStatus": 2,
        "payTime": "2026-08-26T05:01:45.000Z",
    })
    body["recent_messages"] = [{
        "direction": "inbound", "messageType": 26, "content": content,
        "messageId": "status-1", "sentAtMs": 1787579999000, "imageUrls": [],
    }]
    body["envelope"]["payload"].update({
        "remoteMessageId": "status-1", "messageType": 26, "content": content,
    })
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=UnexpectedChat(),
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "authoritative_payment_confirmation_ready"
    assert result["decision"]["actions"][0]["text"] == ReplyTemplates().payment_success_pending_ticket_template
    assert result["decision"]["actions"][0]["rule_governed"] is True


@pytest.mark.asyncio
async def test_platform_system_message_sends_fixed_shipped_reply() -> None:
    class UnexpectedChat:
        async def reply(self, text: str, conversation_id: str) -> str:
            raise AssertionError("platform system message must not enter AI chat")

    body = event_body(event="im.message.received", order={
        "orderId": "order-1", "tenantId": "tenant-1", "accountUnb": "shop-1",
        "buyerUnb": "buyer-1", "chatId": "chat-1", "orderStatus": 3,
        "payTime": "2026-08-26T05:01:45.000Z",
    })
    body["recent_messages"] = [{
        "direction": "inbound", "messageType": 14, "content": "你已发货",
        "messageId": "system-1", "sentAtMs": 1787579999000, "imageUrls": [],
    }]
    body["envelope"]["payload"].update({
        "remoteMessageId": "system-1", "messageType": 14, "content": "你已发货",
    })
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=UnexpectedChat(),
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "authoritative_shipped_status_reply_ready"
    assert result["decision"]["actions"][0]["text"] == ReplyTemplates().order_shipped_template
    assert result["decision"]["actions"][0]["rule_governed"] is True


@pytest.mark.asyncio
async def test_platform_system_event_never_replies_to_an_older_buyer_message() -> None:
    class UnexpectedChat:
        async def reply(self, text: str, conversation_id: str) -> str:
            raise AssertionError("system event must not reuse an older buyer message")

    body = event_body(event="im.message.received")
    body["recent_messages"] = [{
        "direction": "inbound", "messageType": 1, "content": "7排",
        "messageId": "buyer-old", "sentAtMs": 1787579998000, "imageUrls": [],
    }]
    body["envelope"]["payload"].update({
        "remoteMessageId": "system-missing-from-history", "messageType": 14, "content": "你已发货",
    })
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, chat_service=UnexpectedChat(),
    )

    result = await automation.process_event(body)

    assert result == {"decision": {
        "mode": "auto", "actions": [], "reason": "platform_non_buyer_text_deferred",
    }}


@pytest.mark.asyncio
async def test_disabled_shop_blocks_reply_and_price_change() -> None:
    class DisabledShops:
        def is_enabled(self, tenant_id: str, shop_id: str) -> bool:
            assert (tenant_id, shop_id) == ("tenant-1", "shop-1")
            return False

    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, shop_store=DisabledShops(),
    )

    reply = await automation.process_event(event_body(event="im.message.received"))
    price = await automation.process_event(event_body(event="order.created"))

    assert reply["decision"]["reason"] == "shop_automation_disabled"
    assert price["decision"]["reason"] == "shop_automation_disabled"
    assert reply["decision"]["actions"] == price["decision"]["actions"] == []


@pytest.mark.asyncio
async def test_automation_fails_closed_on_identity_mismatch() -> None:
    recognizer = FakeRecognizer(recognition())
    body = event_body()
    body["order"]["buyerUnb"] = "other-buyer"
    automation = PluginAutomation(recognizer, FakeQuoter(exact_quote()), mode="auto", image_loader=load_image)

    result = await automation.process_event(body)

    assert result["decision"]["actions"] == []
    assert result["decision"]["reason"] == "order_session_identity_mismatch"
    assert recognizer.calls == 0


@pytest.mark.asyncio
async def test_image_area_quote_reuses_ticket_count_declared_in_earlier_buyer_text() -> None:
    area_quote = RealQuote(
        quote_scope="area_probe", seat_zone_type="W+", unit_quote_cents=5830,
        needs_ticket_count=True, pricing_source="万达官方会员价",
    )
    body = event_body(event="im.message.received")
    body["envelope"]["payload"]["remoteMessageId"] = "image-current"
    body["recent_messages"] = [
        {"direction": "inbound", "messageType": 1, "content": "两人，可以代买吗", "messageId": "text-1", "sentAtMs": 1787579890000, "imageUrls": []},
        {"direction": "inbound", "messageType": 2, "content": "[图片]", "messageId": "image-current", "sentAtMs": 1787579900000, "imageUrls": ["https://img.alicdn.com/ticket.jpg"]},
    ]
    automation = PluginAutomation(
        FakeRecognizer(recognition().model_copy(update={"selected_seats": [], "selected_count_visible": 0})),
        FakeQuoter(area_quote), mode="auto", image_loader=load_image,
    )

    result = await automation.process_event(body)

    reply = result["decision"]["actions"][0]["text"]
    assert "请告诉我需要几张" not in reply
    assert "58.30" in reply
    assert "¥" not in reply


@pytest.mark.asyncio
async def test_order_created_does_not_requote_recent_wplus_image_without_durable_confirmation() -> None:
    area_quote = RealQuote(
        quote_scope="area_probe", seat_zone_type="W+", unit_quote_cents=6710,
        needs_ticket_count=True, pricing_source="万达官方会员价",
    )
    body = event_body(order={
        "orderId": "order-1", "tenantId": "tenant-1", "accountUnb": "shop-1",
        "buyerUnb": "buyer-1", "chatId": "chat-1", "payment": 17_800,
        "orderStatus": "created", "quantity": 2,
    })
    body["recent_messages"] = [
        {"direction": "inbound", "messageType": 1, "content": "2张 中间W会员位就可以", "messageId": "text-1", "sentAtMs": 1787579890000, "imageUrls": []},
        {"direction": "inbound", "messageType": 2, "content": "[图片]", "messageId": "image-1", "sentAtMs": 1787579900000, "imageUrls": ["https://img.alicdn.com/ticket.jpg"]},
    ]
    automation = PluginAutomation(
        FakeRecognizer(recognition().model_copy(update={"selected_seats": [], "selected_count_visible": 0})),
        FakeQuoter(area_quote), mode="auto", image_loader=load_image,
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "confirmed_quote_unavailable"
    assert result["decision"]["actions"][0]["type"] == "guard_unverified_order"


@pytest.mark.asyncio
async def test_matching_authoritative_order_amount_uses_pending_with_quote_template_without_repricing() -> None:
    body = event_body(order={
        "orderId": "order-1", "tenantId": "tenant-1", "accountUnb": "shop-1",
        "buyerUnb": "buyer-1", "chatId": "chat-1", "payment": 11_660,
        "orderStatus": "created", "quantity": 2,
    })
    templates = ReplyTemplates(
        order_pending_with_quote_template="订单金额已核验为{报价金额}，请核对后付款。",
    )
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(exact_quote()), mode="auto",
        image_loader=load_image, template_provider=lambda: templates,
    )

    result = await automation.process_event(body)

    assert result["decision"]["reason"] == "confirmed_quote_unavailable"
    assert result["decision"]["actions"][0]["type"] == "guard_unverified_order"


@pytest.mark.asyncio
async def test_automation_requires_exact_total_quote() -> None:
    area_quote = RealQuote(
        quote_scope="area_probe", seat_zone_type="W+", unit_quote_cents=5830,
        needs_ticket_count=True, pricing_source="万达官方会员价",
    )
    templates = ReplyTemplates(order_pending_without_quote_template="暂未核验订单金额，请先不要付款。")
    automation = PluginAutomation(
        FakeRecognizer(recognition()), FakeQuoter(area_quote), mode="auto",
        image_loader=load_image, template_provider=lambda: templates,
    )

    result = await automation.process_event(event_body())

    assert result["decision"]["reason"] == "confirmed_quote_unavailable"
    action = result["decision"]["actions"][0]
    assert action["type"] == "guard_unverified_order"
    assert action["unpaid_text"] == "暂未核验订单金额，请先不要付款。"
    assert action["paid_text"] == templates.payment_manual_review_template


def test_successful_verified_price_change_builds_confirmation_only_after_result() -> None:
    succeeded = build_action_result_decision({
        "event_id": "event-1", "action_id": "event-1:change-order-price",
        "result": {"status": "succeeded", "order_id": "order-1", "target_amount_cents": 11660, "verified_amount_cents": 11660},
    }, mode="auto")
    failed = build_action_result_decision({
        "event_id": "event-1", "action_id": "event-1:change-order-price",
        "result": {"status": "failed", "order_id": "order-1", "target_amount_cents": 11660},
    }, mode="auto")
    unknown = build_action_result_decision({
        "event_id": "event-1", "action_id": "event-1:change-order-price",
        "result": {"status": "unknown", "order_id": "order-1", "target_amount_cents": 11660},
    }, mode="auto")
    paid_mismatch = build_action_result_decision({
        "event_id": "event-1", "action_id": "event-1:change-order-price",
        "result": {
            "status": "skipped", "reason_code": "order_already_paid", "order_id": "order-1",
            "target_amount_cents": 11660, "verified_amount_cents": 13980,
            "observed_order_amount_cents": 13980, "auto_refund_eligible": True,
        },
    }, mode="auto")
    manually_changed_amount = build_action_result_decision({
        "event_id": "event-1", "action_id": "event-1:change-order-price",
        "result": {
            "status": "skipped", "reason_code": "order_already_paid", "order_id": "order-1",
            "target_amount_cents": 11660, "verified_amount_cents": 9900,
            "observed_order_amount_cents": 13980, "auto_refund_eligible": False,
            "manual_price_change_suspected": True,
        },
    }, mode="auto")

    assert succeeded["actions"] == [{
        "id": "event-1:confirm-price-change",
        "type": "send_price_change_confirmation",
        "order_id": "order-1",
        "text": "改价已完成，订单金额已调整为116.60元，请在订单页核对后付款。",
        "_completed_action_result": {
            "order_id": "order-1",
            "target_amount_cents": 11660,
            "verified_amount_cents": 11660,
        },
    }]
    assert failed["actions"] == [{
        "id": "event-1:price-change-failed", "type": "send_message",
        "order_id": "order-1", "text": "订单改价未完成：平台改价未完成\n请先不要付款，等待重新核对。",
        "preserve_on_new_buyer_message": True,
        "rule_governed": True,
    }]
    assert unknown["actions"] == [{
        "id": "event-1:price-change-unknown", "type": "send_message",
        "order_id": "order-1", "text": "订单改价未完成：结果尚未完成官方核验\n请先不要付款，等待重新核对。",
        "preserve_on_new_buyer_message": True,
        "rule_governed": True,
    }]
    assert paid_mismatch["actions"][0]["type"] == "cancel_paid_amount_mismatch"
    assert paid_mismatch["actions"][0]["target_amount_cents"] == 11660
    assert paid_mismatch["actions"][0]["observed_order_amount_cents"] == 13980
    assert paid_mismatch["actions"][0]["refund_authorization"] == "unchanged_prechange_amount"
    assert "申请退款" in paid_mismatch["actions"][0]["refund_text"]
    assert manually_changed_amount["actions"] == []


@pytest.mark.parametrize("url", [
    "http://img.alicdn.com/a.jpg",
    "https://evil.example/a.jpg",
    "https://img.alicdn.com.evil.example/a.jpg",
    "https://user:pass@img.alicdn.com/a.jpg",
])
def test_image_url_allowlist_rejects_unsafe_sources(url: str) -> None:
    with pytest.raises(ValueError):
        validate_image_url(url)


def test_image_url_allowlist_accepts_xianyu_cdn() -> None:
    assert validate_image_url("https://img.alicdn.com/a.jpg") == "https://img.alicdn.com/a.jpg"
